import logging
from . import state_helpers
from .library import view as library_view
from .system_menu import view as system_menu_view
from .go_to_page import view as go_to_page_view
from .language import view as language_view
from .book import view as book_view
from .bookmarks import view as bookmarks_view


log = logging.getLogger(__name__)


class Display():
    def __init__(self):
        self.row = 0
        self.hardware_state = []
        self.buffer = []
        self.up_to_date = True

    async def render_to_buffer(self, state, store):
        width, height = state_helpers.dimensions(state)
        location = state['location']
        page_data = None
        if location == 'library':
            page_data = library_view.render(width, height, state)
        elif location == 'system_menu':
            page_data = system_menu_view.render(width, height, state)
        elif location == 'go_to_page':
            page_data = go_to_page_view.render(width, height, state)
        elif location == 'language':
            page_data = language_view.render(width, height, state)
        elif location == 'book':
            page_data = await book_view.render(width, height, state, store)
        elif location == 'bookmarks_menu':
            page_data = await bookmarks_view.render(width, height, state, store)
        if page_data:
            self._set_buffer(page_data)

    async def send_line(self, driver):
        row = self.row
        if row >= len(self.buffer):
            self.up_to_date = True
            return
        self.row += 1
        while row >= len(self.hardware_state):
            self.hardware_state.append([])
        braille = self.buffer[row]
        if braille != self.hardware_state[row]:
            await driver.async_set_braille_row(row, braille)
            self.hardware_state[row] = braille

    def is_up_to_date(self):
        return self.up_to_date

    def _set_buffer(self, data):
        if data != self.buffer:
            self.buffer = data
            self.up_to_date = False
        self.row = 0
