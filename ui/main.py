import os
from frozendict import frozendict
import time
import shutil
import logging
import asyncio
import aioredux
import aioredux.middleware
import async_timeout

from ui.driver_pi import Pi
import ui.utility as utility
import ui.argparser as argparser
import ui.config_loader as config_loader
from ui.setup_logs import setup_logs
from ui.store import main_reducer
from ui.actions import actions
import ui.initial_state as initial_state
import ui.library.handlers as library
import ui.buttons as buttons
from ui.display import Display
from ui.driver_dummy import Dummy

display = Display()


log = logging.getLogger(__name__)


def main():
    args = argparser.parser.parse_args()
    config = config_loader.load()
    log = setup_logs(config, args.loglevel)
    timeout = config.get('comms', 'timeout')

    if args.fuzz_duration:
        log.info('running fuzz test')
        with Dummy(fuzz=True) as driver:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(run_async_timeout(
                driver, config, args.fuzz_duration, loop))
            pending = asyncio.Task.all_tasks()
            loop.run_until_complete(asyncio.gather(*pending))
    elif args.dummy:
        with Dummy(fuzz=False) as driver:
            run(driver, config)
    elif args.emulated and not args.both:
        log.info('running with emulated hardware')
        from ui.driver_emulated import Emulated
        with Emulated(delay=args.delay, display_text=args.text) as driver:
            run(driver, config)
    elif args.emulated and args.both:
        log.info('running with both emulated and real hardware on port %s'
                 % args.tty)
        from ui.driver_both import DriverBoth
        with DriverBoth(port=args.tty, pi_buttons=args.pi_buttons,
                        delay=args.delay, display_text=args.text,
                        timeout=timeout) as driver:
            run(driver, config)
    else:
        log.info('running with real hardware on port %s, timeout %s' %
                 (args.tty, timeout))
        with Pi(port=args.tty,
                pi_buttons=args.pi_buttons,
                timeout=timeout) as driver:
            run(driver, config)


def run(driver, config):
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_async(driver, config, loop))
    pending = asyncio.Task.all_tasks()
    loop.run_until_complete(asyncio.gather(*pending))


async def run_async_timeout(driver, config, duration, loop):
    try:
        with async_timeout.timeout(duration, loop=loop):
            await run_async(driver, config, loop)
    except asyncio.TimeoutError:
        return


async def run_async(driver, config, loop):

    state = initial_state.read()
    width, height = driver.get_dimensions()
    state = state.copy(app=state['app'].copy(
        display=frozendict({'width': width, 'height': height})))

    thunk_middleware = aioredux.middleware.thunk_middleware
    create_store = aioredux.apply_middleware(
        thunk_middleware)(aioredux.create_store)
    store = await create_store(main_reducer, state)

    library_dir = config.get('files', 'library_dir')
    await library.sync(state['app'], library_dir, store)

    store.subscribe(handle_changes(driver, config, store))

    # if we startup and update_ui is still 'in progress' then we are using the
    # old state file and update has failed
    if state['app']['update_ui'] == 'in progress':
        await store.dispatch(actions.update_ui('failed'))

    # since handle_changes subscription happens after init and library.sync it
    # may not have triggered. so we trigger it here. if we put it before init
    # it will start of by rendering a possibly invalid state. library.sync
    # won't dispatch if the library is already in sync so there would be no
    # guarantee of the subscription triggering if subscribed before that.
    await store.dispatch(actions.trigger())

    while 1:
        state = store.state
        if (await handle_hardware(driver, state, store)):
            break
        await buttons.check(driver, state['app'],
                            store.dispatch)
        await display.send_line(driver)
        await asyncio.sleep(0.001)


def handle_changes(driver, config, store):
    def listener():
        state = store.state
        asyncio.ensure_future(display.render_to_buffer(state['app'], store))
        asyncio.ensure_future(fully_load_books(state['app'], store))
        asyncio.ensure_future(change_files(config, state['app'], store))
        asyncio.ensure_future(initial_state.write(state))
    return listener


async def fully_load_books(state, store):
    state = store.state['app']
    if state['load_books'] == 'start':
        await store.dispatch(actions.load_books('loading'))
        books = state['user']['books']
        log.info('loading {} books'.format(len(books)))
        for book in books:
            if not book.pages:
                state = store.state['app']
                if state['load_books'] == 'cancel':
                    log.info('cancelling loading books')
                    break
                try:
                    book = tuple(filter(lambda b: b.filename ==
                                        book.filename, state['user']['books']))[0]
                except IndexError:
                    continue
                if book.loading:
                    log.info('already loading {}, cancelling'.format(book.title))
                    continue
                await store.dispatch(actions.set_book_loading(book))
                book = book.read_pages()
                await store.dispatch(actions.add_or_replace(book))
                await asyncio.sleep(1)
        await store.dispatch(actions.load_books(False))
        log.info('loading books done')


async def change_files(config, state, store):
    state = store.state['app']
    if state['replacing_library'] == 'start':
        await store.dispatch(actions.load_books('cancel'))
        await store.dispatch(actions.replace_library('in progress'))
        await library.replace(config, state, store)
        await store.dispatch(actions.replace_library(False))
    if state['backing_up_log'] == 'start':
        await store.dispatch(actions.backup_log('in progress'))
        backup_log(config)
        await store.dispatch(actions.backup_log('done'))
    if state['update_ui'] == 'start':
        log.info('update ui = start')
        if utility.find_ui_update(config):
            await store.dispatch(actions.update_ui('in progress'))
        else:
            log.info('update not found')
            await store.dispatch(actions.update_ui('failed'))


async def handle_hardware(driver, state, store):
    if not driver.is_ok():
        log.debug('shutting down due to GUI closed')
        await store.dispatch(actions.load_books('cancel'))
        await initial_state.write(state)
        await store.dispatch(actions.shutdown())
    if state['app']['shutting_down']:
        if isinstance(driver, Pi):
            driver.clear_page()
            driver.lower_rods()
            os.system('sudo shutdown -h now')
        # never exit from Dummy driver
        elif isinstance(driver, Dummy):
            return False
        return True
    elif state['hardware']['resetting_display'] == 'start':
        store.dispatch(actions.reset_display('in progress'))
        driver.reset_display()
        display.hardware_state = []
        await store.dispatch(actions.reset_display('done'))
    elif state['hardware']['warming_up'] == 'start':
        store.dispatch(actions.warm_up('in progress'))
        driver.warm_up()
        await store.dispatch(actions.warm_up(False))


def backup_log(config):
    usb_dir = config.get('files', 'usb_dir')
    log_file = config.get('files', 'log_file')
    # make a filename based on the date
    backup_file = os.path.join(usb_dir, time.strftime('%Y%m%d_log.txt'))
    log.warning('backing up log to USB stick: {}'.format(backup_file))
    try:
        shutil.copyfile(log_file, backup_file)
    except IOError as e:
        log.warning("couldn't backup log file: {}".format(e))


if __name__ == '__main__':
    main()
