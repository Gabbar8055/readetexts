#! /usr/bin/env python

# Copyright (C) 2008, 2009 James D. Simmons
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
import os
import logging
import tempfile
import time
import zipfile
import pygtk
pygtk.require('2.0')
import gtk
import string
from sugar.graphics import style
from sugar import profile
from sugar.activity import activity
from sugar import network
from sugar.datastore import datastore
from sugar.graphics.alert import NotifyAlert
from readtoolbar import ReadToolbar, ViewToolbar, EditToolbar,  BooksToolbar,  SpeechToolbar
from readsidebar import Sidebar
from gettext import gettext as _
import pango
import dbus
import gobject
import telepathy
import cPickle as pickle

import speech
import xopower

PAGE_SIZE = 38
TOOLBAR_READ = 2
TOOLBAR_BOOKS = 3
COLUMN_TITLE = 0
COLUMN_AUTHOR = 1
COLUMN_PATH = 2

logger = logging.getLogger('read-etexts-activity')

class Annotations():
    
    def __init__(self,  pickle_file_name):
        self.title = ''
        self.notes = {0:''}
        self.bookmarks = []
        self.highlights = {0:  [] }
        self.pickle_file_name = pickle_file_name

    def get_title(self):
        return self.title
        
    def set_title(self,  title):
        self.title = title
    
    def get_notes(self):
        return self.notes
        
    def get_note(self,  page):
        try:
            return self.notes[page]
        except KeyError:
            return ''
        
    def add_note(self,  page,  text):
        self.notes[page] = text
        if text == '':
            del self.notes[page]

    def is_bookmarked(self,  page):
        bookmark = self.bookmarks.count(page)
        if bookmark > 0:
            return True
        else:
            return False

    def add_bookmark(self,  page):
        self.bookmarks.append(page)
        
    def remove_bookmark(self,  page):
        try:
            self.bookmarks.remove(page)
            # print 'bookmarks=',  self.bookmarks
        except ValueError:
            print 'page already not bookmarked',  page

    def get_bookmarks(self):
        self.bookmarks.sort()
        return self.bookmarks
        
    def get_highlights(self,  page):
        try:
            return self.highlights[page]
        except KeyError:
            return []
            
    def set_highlights(self,  page,  tuples_list):
        self.highlights[page] = tuples_list
        if tuples_list == []:
            del self.highlights[page]
        
    def restore(self):
        if os.path.exists(self.pickle_file_name):
            pickle_input = open(self.pickle_file_name,  'rb')
            self.title = pickle.load(pickle_input)
            self.bookmarks = pickle.load(pickle_input)
            self.notes = pickle.load(pickle_input)
            self.highlights = pickle.load(pickle_input)
            pickle_input.close()

    def save(self):
        pickle_output = open(self.pickle_file_name,  'wb')
        pickle.dump(self.title,  pickle_output)
        pickle.dump(self.bookmarks,  pickle_output)
        pickle.dump(self.notes,  pickle_output)
        pickle.dump(self.highlights,  pickle_output)
        pickle_output.close()

class ReadHTTPRequestHandler(network.ChunkedGlibHTTPRequestHandler):
    """HTTP Request Handler for transferring document while collaborating.

    RequestHandler class that integrates with Glib mainloop. It writes
    the specified file to the client in chunks, returning control to the
    mainloop between chunks.

    """
    def translate_path(self, path):
        """Return the filepath to the shared document."""
        return self.server.filepath


class ReadHTTPServer(network.GlibTCPServer):
    """HTTP Server for transferring document while collaborating."""
    def __init__(self, server_address, filepath):
        """Set up the GlibTCPServer with the ReadHTTPRequestHandler.

        filepath -- path to shared document to be served.
        """
        self.filepath = filepath
        network.GlibTCPServer.__init__(self, server_address,
                                       ReadHTTPRequestHandler)


class ReadURLDownloader(network.GlibURLDownloader):
    """URLDownloader that provides content-length and content-type."""

    def get_content_length(self):
        """Return the content-length of the download."""
        if self._info is not None:
            return int(self._info.headers.get('Content-Length'))

    def get_content_type(self):
        """Return the content-type of the download."""
        if self._info is not None:
            return self._info.headers.get('Content-type')
        return None

READ_STREAM_SERVICE = 'read-activity-http'

class ReadEtextsActivity(activity.Activity):
    def __init__(self, handle):
        "The entry point to the Activity"
        gtk.gdk.threads_init()
        self.current_word = 0
        self.word_tuples = []
        
        activity.Activity.__init__(self, handle)
        self.connect('delete-event', self.delete_cb)
        
        self.fileserver = None
        self.object_id = handle.object_id
        self.extra_journal_entry = None
       
        toolbox = activity.ActivityToolbox(self)
        activity_toolbar = toolbox.get_activity_toolbar()
        activity_toolbar.remove(activity_toolbar.keep)
        activity_toolbar.keep = None
        self.set_toolbox(toolbox)
        
        self.edit_toolbar = EditToolbar()
        self.edit_toolbar.undo.props.visible = False
        self.edit_toolbar.redo.props.visible = False
        self.edit_toolbar.separator.props.visible = False
        self.edit_toolbar.copy.set_sensitive(False)
        self.edit_toolbar.copy.connect('clicked', self.edit_toolbar_copy_cb)
        self.edit_toolbar.paste.props.visible = False
        toolbox.add_toolbar(_('Edit'), self.edit_toolbar)
        self.edit_toolbar.set_activity(self)
        self.edit_toolbar.show()
        
        self.read_toolbar = ReadToolbar()
        toolbox.add_toolbar(_('Read'), self.read_toolbar)
        self.read_toolbar.set_activity(self)
        self.read_toolbar.show()

        if not self._shared_activity and self._object_id is None:
            self.books_toolbar = BooksToolbar()
            toolbox.add_toolbar(_('Books'), self.books_toolbar)
            self.books_toolbar.set_activity(self)
            self.books_toolbar.show()

        self.view_toolbar = ViewToolbar()
        toolbox.add_toolbar(_('View'), self.view_toolbar)
        self.view_toolbar.connect('go-fullscreen',
                self.view_toolbar_go_fullscreen_cb)
        self.view_toolbar.set_activity(self)
        self.view_toolbar.show()

        if speech.supported:
            self.speech_toolbar = SpeechToolbar()
            toolbox.add_toolbar(_('Speech'), self.speech_toolbar)
            self.speech_toolbar.set_activity(self)
            self.speech_toolbar.show()

        toolbox.show()
        self.scrolled = gtk.ScrolledWindow()
        self.scrolled.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
        self.scrolled.props.shadow_type = gtk.SHADOW_NONE

        self.textview = gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_cursor_visible(False)
        self.textview.set_left_margin(50)
        self.textview.connect("key_press_event", self.keypress_cb)

        self.annotation_textview = gtk.TextView()
        self.annotation_textview.set_left_margin(50)
        self.annotation_textview.set_right_margin(50)
        self.annotation_textview.set_wrap_mode(gtk.WRAP_WORD)

        buffer = self.textview.get_buffer()
        buffer.connect("mark-set", self.mark_set_cb)
        self.font_desc = pango.FontDescription("sans %d" % style.zoom(10))
        self.textview.modify_font(self.font_desc)
        self.annotation_textview.modify_font(self.font_desc)
        self.scrolled.add(self.textview)
        self.textview.show()
        self.scrolled.show()
        v_adjustment = self.scrolled.get_vadjustment()
        self.clipboard = gtk.Clipboard(display=gtk.gdk.display_get_default(), selection="CLIPBOARD")
        self.page = 0
        self.textview.grab_focus()

        self.ls = gtk.ListStore(gobject.TYPE_STRING, gobject.TYPE_STRING, gobject.TYPE_STRING)
        tv = gtk.TreeView(self.ls)
        tv.set_rules_hint(True)
        tv.set_search_column(COLUMN_TITLE)
        selection = tv.get_selection()
        selection.set_mode(gtk.SELECTION_SINGLE)
        selection.connect("changed", self.selection_cb)
        renderer = gtk.CellRendererText()
        col = gtk.TreeViewColumn('Title', renderer, text=COLUMN_TITLE)
        col.set_sort_column_id(COLUMN_TITLE)
        tv.append_column(col)
    
        renderer = gtk.CellRendererText()
        col = gtk.TreeViewColumn('Author', renderer, text=COLUMN_AUTHOR)
        col.set_sort_column_id(COLUMN_AUTHOR)
        tv.append_column(col)

        self.list_scroller = gtk.ScrolledWindow(hadjustment=None, vadjustment=None)
        self.list_scroller.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        self.list_scroller.add(tv)
        
        self.progressbar = gtk.ProgressBar()
        self.progressbar.set_orientation(gtk.PROGRESS_LEFT_TO_RIGHT)
        self.progressbar.set_fraction(0.0)
        
        vbox = gtk.VBox()
        vbox.pack_start(self.progressbar,  False,  False,  10)
        vbox.pack_start(self.scrolled)
        vbox.pack_end(self.list_scroller)
        vbox.pack_end(self.annotation_textview,  False,  False,  10)
        tv.show()
        vbox.show()
        self.list_scroller.hide()
        self.annotation_textview.show()

        self.sidebar = Sidebar()
        self.sidebar.show()

        sidebar_hbox = gtk.HBox()
        sidebar_hbox.pack_start(self.sidebar, expand=False, fill=False)
        sidebar_hbox.pack_start(vbox,  expand=True, fill=True)
        self.set_canvas(sidebar_hbox)
        sidebar_hbox.show()

        textbuffer = self.textview.get_buffer()
        self.tag = textbuffer.create_tag()
        self.tag.set_property('weight', pango.WEIGHT_BOLD)
        self.tag.set_property( 'foreground', "white")
        self.tag.set_property( 'background', "black")

        self.underline_tag = textbuffer.create_tag()
        self.underline_tag.set_property('underline', 'single')
        self.underline_tag.set_property( 'foreground', 'black')
        self.underline_tag.set_property( 'background', 'yellow')

        self.pickle_file_temp = os.path.join(self.get_activity_root(),  'instance', 'pkl%i' % time.time())
        self.annotations = Annotations(self.pickle_file_temp)

        xopower.setup_idle_timeout()
        if xopower.service_activated:
            self.scrolled.props.vadjustment.connect("value-changed", self.user_action_cb)
            self.scrolled.props.hadjustment.connect("value-changed", self.user_action_cb)
            self.connect("focus-in-event", self.focus_in_event_cb)
            self.connect("focus-out-event", self.focus_out_event_cb)
            self.connect("notify::active", self.now_active_cb)
    
        # start on the read toolbar
        self.toolbox.set_current_toolbar(TOOLBAR_READ)
        self.unused_download_tubes = set()
        self.want_document = True
        self.download_content_length = 0
        self.download_content_type = None
        # Status of temp file used for write_file:
        self.tempfile = None
        self.close_requested = False
        self.connect("shared", self.shared_cb)
        h = hash(self._activity_id)
        self.port = 1024 + (h % 64511)

        self.is_received_document = False
        
        if self._shared_activity and handle.object_id == None:
            # We're joining, and we don't already have the document.
            if self.get_shared():
                # Already joined for some reason, just get the document
                self.joined_cb(self)
            else:
                # Wait for a successful join before trying to get the document
                self.connect("joined", self.joined_cb)
        elif self.object_id is None:
            # Not joining, not resuming
            self.toolbox.set_current_toolbar(TOOLBAR_BOOKS)
            f = open("help.txt","r")
            line = ''
            label_text = ''
            while True:
                line = f.readline()
                if not line:
                    break
                else:
                    label_text = label_text + unicode(line,  "iso-8859-1")
            textbuffer = self.textview.get_buffer()
            textbuffer.set_text(label_text)
            f.close()

        speech.highlight_cb = self.highlight_next_word
        speech.reset_cb = self.reset_play_button
 
    def reset_current_word(self):
        self.current_word = 0
        
    def reset_play_button(self):
        self.reset_current_word()
        play = self.speech_toolbar.play_btn
        play.set_active(False)
        self.textview.grab_focus()

    def delete_cb(self, widget, event):
        speech.stop()
        return False

    def highlight_next_word(self,  word_count):
        if word_count < len(self.word_tuples) :
            word_tuple = self.word_tuples[word_count]
            textbuffer = self.textview.get_buffer()
            iterStart = textbuffer.get_iter_at_offset(word_tuple[0])
            iterEnd = textbuffer.get_iter_at_offset(word_tuple[1])
            bounds = textbuffer.get_bounds()
            textbuffer.remove_all_tags(bounds[0], bounds[1])
            textbuffer.apply_tag(self.tag, iterStart, iterEnd)
            v_adjustment = self.scrolled.get_vadjustment()
            max = v_adjustment.upper - v_adjustment.page_size
            max = max * word_count
            max = max / len(self.word_tuples)
            v_adjustment.value = max
            self.current_word = word_count
        return True

    def mark_set_cb(self, textbuffer, iter, textmark):
        self.read_toolbar.update_underline_button(False) 

        if textbuffer.get_has_selection():
            buffer = self.textview.get_buffer()
            begin, end = buffer.get_selection_bounds()
            underline_tuple = [begin.get_offset(),  end.get_offset()]
            tuples_list =  self.annotations.get_highlights(self.page)
            count = 0
            while count < len(tuples_list) :
                compare_tuple = tuples_list[count]
                if underline_tuple[0] >= compare_tuple[0] and underline_tuple[1] <= compare_tuple[1]:
                    self.read_toolbar.update_underline_button(True) 
                    break
                count = count + 1

            self.edit_toolbar.copy.set_sensitive(True)
            self.read_toolbar.underline.props.sensitive = True
        else:
            self.edit_toolbar.copy.set_sensitive(False)
            self.read_toolbar.underline.props.sensitive = False

    def edit_toolbar_copy_cb(self, button):
        copy_text = buffer.get_text(begin, end)
        self.clipboard.set_text(copy_text)

    def view_toolbar_go_fullscreen_cb(self, view_toolbar):
        self.fullscreen()

    def hide_table_keypress_cb(self, widget, event):
        if keyname == 'Escape':
            self.list_scroller.hide()
            return True
        return False

    def keypress_cb(self, widget, event):
        "Respond when the user presses one of the arrow keys"
        if xopower.service_activated:
            xopower.reset_sleep_timer()
        keyname = gtk.gdk.keyval_name(event.keyval)
        if keyname == 'KP_End' and speech.supported:
            play = self.speech_toolbar.play_btn
            play.set_active(int(not play.get_active()))
            return True
        if keyname == 'plus':
            self.font_increase()
            return True
        if keyname == 'minus':
            self.font_decrease()
            return True
        if keyname == 'Escape':
            self.list_scroller.hide()
            return True
        if speech.supported and speech.is_stopped() == False:
            # If speech is in progress, ignore other keys.
            return True
        if keyname == 'KP_Right':
            self.scroll_down()
            return True
        if keyname == 'Page_Up':
            self.page_previous()
            return True
        if keyname == 'KP_Left':
            self.scroll_up()
            return True
        if keyname == 'Page_Down' :
            self.page_next()
            return True
        if keyname == 'Up'or keyname == 'KP_Up':
            self.scroll_up()
            return True
        if keyname == 'Down' or keyname == 'KP_Down':
            self.scroll_down()
            return True
        return False
        
    def bookmarker_clicked(self,  button):
        if button.get_active() == True:
            self.annotations.add_bookmark(self.page)
        else:
            self.annotations.remove_bookmark(self.page)
        self.show_bookmark_state()

    def show_bookmark_state(self):
        bookmark = self.annotations.is_bookmarked(self.page)
        if bookmark == True:
            self.sidebar.show_bookmark_icon(True)
            self.read_toolbar.update_bookmark_button(True)
        else:
            self.sidebar.show_bookmark_icon(False)
            self.read_toolbar.update_bookmark_button(False)

    def underline_clicked(self,  button):
        tuples_list =  self.annotations.get_highlights(self.page)
        buffer = self.textview.get_buffer()
        begin, end = buffer.get_selection_bounds()
        underline_tuple = [begin.get_offset(),  end.get_offset()]

        if button.get_active() == True:
            tuples_list.append(underline_tuple)
            self.annotations.set_highlights(self.page,  tuples_list)
        else:
            buffer = self.textview.get_buffer()
            begin, end = buffer.get_selection_bounds()
            underline_tuple = [begin.get_offset(),  end.get_offset()]
            tuples_list =  self.annotations.get_highlights(self.page)
            count = 0
            while count < len(tuples_list) :
                compare_tuple = tuples_list[count]
                if underline_tuple[0] >= compare_tuple[0] and underline_tuple[1] <= compare_tuple[1]:
                    tuples_list.remove(compare_tuple)
                    self.annotations.set_highlights(self.page,  tuples_list)
                    break
                count = count + 1
        self.show_underlines()

    def show_underlines(self):
        tuples_list =  self.annotations.get_highlights(self.page)
        textbuffer = self.textview.get_buffer()
        bounds = textbuffer.get_bounds()
        textbuffer.remove_all_tags(bounds[0], bounds[1])
        count = 0
        while count < len(tuples_list) :
            underline_tuple = tuples_list[count]
            iterStart = textbuffer.get_iter_at_offset(underline_tuple[0])
            iterEnd = textbuffer.get_iter_at_offset(underline_tuple[1])
            textbuffer.apply_tag(self.underline_tag, iterStart, iterEnd)
            count = count + 1

    def prev_bookmark(self):
        bookmarks = self.annotations.get_bookmarks()
        count = len(bookmarks) - 1
        while count >= 0:
            if bookmarks[count] < self.page:
                self.page = bookmarks[count]
                self.show_page(self.page)
                self.read_toolbar.set_current_page(self.page)
                return
            count = count - 1
        # if we're before the first bookmark wrap to the last.
        if len(bookmarks) > 0:
            self.page = bookmarks[len(bookmarks) - 1]
            self.show_page(self.page)
            self.read_toolbar.set_current_page(self.page)

    def next_bookmark(self):
        bookmarks = self.annotations.get_bookmarks()
        count = 0
        while count < len(bookmarks):
            if bookmarks[count] > self.page:
                self.page = bookmarks[count]
                self.show_page(self.page)
                self.read_toolbar.set_current_page(self.page)
                return
            count = count + 1
        # if we're after the last bookmark wrap to the first.
        if len(bookmarks) > 0:
            self.page = bookmarks[0]
            self.show_page(self.page)
            self.read_toolbar.set_current_page(self.page)

    def page_next(self):
        textbuffer = self.annotation_textview.get_buffer()
        self.annotations.add_note(self.page,  textbuffer.get_text(textbuffer.get_start_iter(),  textbuffer.get_end_iter()))
        self.page = self.page + 1
        if self.page >= len(self.page_index): self.page=len(self.page_index) - 1
        self.show_page(self.page)
        v_adjustment = self.scrolled.get_vadjustment()
        v_adjustment.value = v_adjustment.lower
        self.read_toolbar.set_current_page(self.page)

    def page_previous(self):
        textbuffer = self.annotation_textview.get_buffer()
        self.annotations.add_note(self.page,  textbuffer.get_text(textbuffer.get_start_iter(),  textbuffer.get_end_iter()))
        self.page=self.page-1
        if self.page < 0: self.page=0
        self.show_page(self.page)
        v_adjustment = self.scrolled.get_vadjustment()
        v_adjustment.value = v_adjustment.upper - v_adjustment.page_size
        self.read_toolbar.set_current_page(self.page)

    def font_decrease(self):
        font_size = self.font_desc.get_size() / 1024
        font_size = font_size - 1
        if font_size < 1:
            font_size = 1
        self.font_desc.set_size(font_size * 1024)
        self.textview.modify_font(self.font_desc)
        self.annotation_textview.modify_font(self.font_desc)

    def font_increase(self):
        font_size = self.font_desc.get_size() / 1024
        font_size = font_size + 1
        self.font_desc.set_size(font_size * 1024)
        self.textview.modify_font(self.font_desc)
        self.annotation_textview.modify_font(self.font_desc)

    def scroll_down(self):
        v_adjustment = self.scrolled.get_vadjustment()
        if v_adjustment.value == v_adjustment.upper - v_adjustment.page_size:
            self.page_next()
            return
        if v_adjustment.value < v_adjustment.upper - v_adjustment.page_size:
            new_value = v_adjustment.value + v_adjustment.step_increment
            if new_value > v_adjustment.upper - v_adjustment.page_size:
                new_value = v_adjustment.upper - v_adjustment.page_size
            v_adjustment.value = new_value

    def scroll_up(self):
        v_adjustment = self.scrolled.get_vadjustment()
        if v_adjustment.value == v_adjustment.lower:
            self.page_previous()
            return
        if v_adjustment.value > v_adjustment.lower:
            new_value = v_adjustment.value - v_adjustment.step_increment
            if new_value < v_adjustment.lower:
                new_value = v_adjustment.lower
            v_adjustment.value = new_value

    def set_current_page(self, page):
        self.page = page

    def show_page(self, page_number):
        self.show_bookmark_state()
        position = self.page_index[page_number]
        self.reset_current_word()
        self.etext_file.seek(position)
        linecount = 0
        label_text = '\n\n\n'
        while linecount < PAGE_SIZE:
            line = self.etext_file.readline()
            if not line:
                break
            else:
                label_text = label_text + unicode(line,  "iso-8859-1")
            linecount = linecount + 1
        textbuffer = self.textview.get_buffer()
        label_text = label_text + '\n\n\n'
        textbuffer.set_text(label_text)
        annotation_textbuffer = self.annotation_textview.get_buffer()
        annotation_textbuffer.set_text(self.annotations.get_note(page_number))
        self.show_underlines()
        self.prepare_highlighting(label_text)

    def prepare_highlighting(self, label_text):
        i = 0
        j = 0
        word_begin = 0
        word_end = 0
        ignore_chars = [' ',  '\n',  u'\r',  '_',  '[', '{', ']', '}', '|',  '<',  '>',  '*',  '+',  '/',  '\\' ]
        ignore_set = set(ignore_chars)
        self.word_tuples = []
        while i < len(label_text):
            if label_text[i] not in ignore_set:
                word_begin = i
                j = i
                while  j < len(label_text) and label_text[j] not in ignore_set:
                    j = j + 1
                    word_end = j
                    i = j
                word_tuple = (word_begin, word_end, label_text[word_begin: word_end])
                if word_tuple[2] != u'\r':
                    self.word_tuples.append(word_tuple)
            i = i + 1

    def add_word_marks(self):
        "Adds a mark between each word of text."
        i = self.current_word
        marked_up_text  = '<speak> '
        while i < len(self.word_tuples):
            word_tuple = self.word_tuples[i]
            marked_up_text = marked_up_text + '<mark name="' + str(i) + '"/>' + word_tuple[2]
            i = i + 1
        return marked_up_text + '</speak>'

    def show_found_page(self, page_tuple):
        self.show_bookmark_state()
        position = self.page_index[page_tuple[0]]
        self.etext_file.seek(position)
        linecount = 0
        label_text = '\n\n\n'
        while linecount < PAGE_SIZE:
            line = self.etext_file.readline()
            if not line:
               break
            else:
                label_text = label_text + unicode(line, "iso-8859-1")
                linecount = linecount + 1
        label_text = label_text + '\n\n\n'
        textbuffer = self.textview.get_buffer()
        tag = textbuffer.create_tag()
        tag.set_property('weight', pango.WEIGHT_BOLD)
        tag.set_property( 'foreground', "white")
        tag.set_property( 'background', "black")
        textbuffer.set_text(label_text)
        iterStart = textbuffer.get_iter_at_offset(page_tuple[1])
        iterEnd = textbuffer.get_iter_at_offset(page_tuple[2])
        textbuffer.apply_tag(tag, iterStart, iterEnd)
        self.edit_toolbar.update_find_buttons()

    def save_extracted_file(self, zipfile, filename):
        "Extract the file to a temp directory for viewing"
        filebytes = zipfile.read(filename)
        outfn = self.make_new_filename(filename)
        if (outfn == ''):
            return False
        f = open(os.path.join(self.get_activity_root(), 'instance',  outfn),  'w')
        try:
            f.write(filebytes)
        finally:
            f.close

    def extract_pickle_file(self):
        "Extract the pickle file to an instance directory for viewing"
        try:
            self.zf.getinfo('annotations.pkl')
            filebytes = self.zf.read('annotations.pkl')
            f = open(self.pickle_file_temp,  'wb')
            try:
                f.write(filebytes)
            finally:
                f.close
            return True
        except KeyError:
            return False

    def read_file(self, file_path):
        """Load a file from the datastore on activity start"""
        logger.debug('ReadEtextsActivity.read_file: %s', file_path)
        tempfile = os.path.join(self.get_activity_root(),  'instance', 'tmp%i' % time.time())
        os.link(file_path,  tempfile)
        self.tempfile = tempfile
        self.load_document(self.tempfile)

    def make_new_filename(self, filename):
        partition_tuple = filename.rpartition('/')
        return partition_tuple[2]

    def get_saved_page_number(self):
        title = self.metadata.get('title', '')
        if title == ''  or not title[len(title)- 1].isdigit():
            self.page = 0
        else:
            i = len(title) - 1
            page = ''
            while (title[i].isdigit() and i > 0):
                page = title[i] + page
                i = i - 1
            if title[i] == 'P':
                self.page = int(page) - 1
            else:
                # not a page number; maybe a volume number.
                self.page = 0
        
    def save_page_number(self):
        title = self.metadata.get('title', '')
        if title == ''  or not title[len(title)- 1].isdigit():
            title = title + ' P' +  str(self.page + 1)
        else:
            i = len(title) - 1
            while (title[i].isdigit() and i > 0):
                i = i - 1
            if title[i] == 'P':
                title = title[0:i] + 'P' + str(self.page + 1)
            else:
                title = title + ' P' + str(self.page + 1)
        self.metadata['title'] = title

    def load_document(self, filename):
        "Read the Etext file"
        if zipfile.is_zipfile(filename):
            self.zf = zipfile.ZipFile(filename, 'r')
            self.book_files = self.zf.namelist()
            i = 0
            current_file_name = 'no file'
            while (i < len(self.book_files)):
                if (self.book_files[i] != 'annotations.pkl'):
                    self.save_extracted_file(self.zf, self.book_files[i]) 
                    current_file_name = os.path.join(self.get_activity_root(), 'instance',  \
                                                     self.make_new_filename(self.book_files[i]))
                else:
                    self.extract_pickle_file()
                i = i + 1
        else:
            current_file_name = filename
            
        self.etext_file = open(current_file_name,"r")
        
        self.page_index = [ 0 ]
        pagecount = 0
        linecount = 0
        while self.etext_file:
            line = self.etext_file.readline()
            if not line:
                break
            linecount = linecount + 1
            if linecount >= PAGE_SIZE:
                position = self.etext_file.tell()
                self.page_index.append(position)
                linecount = 0
                pagecount = pagecount + 1

        self.annotations.restore()
        if self.is_received_document == True:
            self.metadata['title'] = self.annotations.get_title()
            self.metadata['title_set_by_user'] = '1'
            print self.annotations.get_title()
            
        self.get_saved_page_number()
        self.show_page(self.page)
        self.read_toolbar.set_total_pages(pagecount + 1)
        self.read_toolbar.set_current_page(self.page)
        if filename.endswith(".zip"):
            os.remove(current_file_name)
            
        # We've got the document, so if we're a shared activity, offer it
        if self.get_shared():
            self.watch_for_tubes()
            self.share_document()

    def rewrite_zip(self):
        if zipfile.is_zipfile(self.tempfile):
            new_zipfile = os.path.join(self.get_activity_root(), 'instance',
                    'rewrite%i' % time.time())
            print self.tempfile,  new_zipfile
            zf_new = zipfile.ZipFile(new_zipfile, 'w')
            zf_old = zipfile.ZipFile(self.tempfile, 'r')
            book_files = self.zf.namelist()
            i = 0
            while (i < len(book_files)):
                if (book_files[i] != 'annotations.pkl'):
                    self.save_extracted_file(zf_old, book_files[i])
                    outfn = self.make_new_filename(book_files[i])
                    fname = os.path.join(self.get_activity_root(), 'instance',  outfn)
                    zf_new.write(fname.encode( "utf-8" ),  outfn.encode( "utf-8" ))
                    print 'rewriting',  outfn
                    os.remove(fname)
                i = i + 1
            zf_new.write(self.pickle_file_temp,  'annotations.pkl')
        
            zf_old.close()
            zf_new.close()
            os.remove(self.tempfile)
            self.tempfile = new_zipfile
        else:
            new_zipfile = os.path.join(self.get_activity_root(), 'instance',
                    'rewrite%i' % time.time())
            print self.tempfile,  new_zipfile
            zf_new = zipfile.ZipFile(new_zipfile, 'w')
            outfn = self.make_new_filename(self.tempfile)
            zf_new.write(self.tempfile,  outfn)
            print 'adding',  outfn
            zf_new.write(self.pickle_file_temp,  'annotations.pkl')
            zf_new.close()
            os.remove(self.tempfile)
            self.tempfile = new_zipfile

    def write_file(self, filename):
        "Save meta data for the file."
        if self.is_received_document == True:
            # This document was given to us by someone, so we have
            # to save it to the Journal.
            self.etext_file.seek(0)
            filebytes = self.etext_file.read()
            print 'saving shared document'
            f = open(filename, 'wb')
            try:
                f.write(filebytes)
            finally:
                f.close
        elif self.tempfile:
            if self.close_requested:
                textbuffer = self.annotation_textview.get_buffer()
                self.annotations.add_note(self.page,  textbuffer.get_text(textbuffer.get_start_iter(),  textbuffer.get_end_iter()))
                title = self.metadata.get('title', '')
                self.annotations.set_title(str(title))
                self.annotations.save()
                self.rewrite_zip()
                os.link(self.tempfile,  filename)
                logger.debug("Removing temp file %s because we will close", self.tempfile)
                os.unlink(self.tempfile)
                os.remove(self.pickle_file_temp)
                self.tempfile = None
                self.pickle_file_temp = None
        else:
            # skip saving empty file
            raise NotImplementedError
        # The last book we downloaded has 2 journal entries.  Delete the other one.
        if self.extra_journal_entry != None and self.close_requested:
            datastore.delete(self.extra_journal_entry.object_id)

        self.metadata['activity'] = self.get_bundle_id()
        self.metadata['mime_type'] = 'application/zip'
        self.save_page_number()

    def can_close(self):
        self.close_requested = True
        return True

    def selection_cb(self, selection):
        self.clear_downloaded_bytes()
        tv = selection.get_tree_view()
        model = tv.get_model()
        sel = selection.get_selected()
        if sel:
            model, iter = sel
            self.selected_title = model.get_value(iter,COLUMN_TITLE)
            self.selected_author = model.get_value(iter,COLUMN_AUTHOR)
            self.selected_path = model.get_value(iter,COLUMN_PATH)
            self.books_toolbar.enable_button(True)

    def find_books(self, search_text):
        self.clear_downloaded_bytes()
        self.books_toolbar.enable_button(False)
        self.list_scroller.hide()
        self.list_scroller_visible = False
        self.book_selected = False
        self.ls.clear()
        search_tuple = search_text.lower().split()
        if len(search_tuple) == 0:
            self.alert(_('Error'), _('You must enter at least one search word.'))
            self.books_toolbar.search_entry.grab_focus()
            return
        f = open('bookcatalog.txt', 'r')
        while f:
            line = unicode(f.readline(), "iso-8859-1")
            if not line:
                break
            line_lower = line.lower()
            i = 0
            words_found = 0
            while i < len(search_tuple):
                text_index = line_lower.find(search_tuple[i]) 
                if text_index > -1:
                    words_found = words_found + 1
                i = i + 1
            if words_found == len(search_tuple):
                iter = self.ls.append()
                book_tuple = line.split('|')
                self.ls.set(iter, COLUMN_TITLE, book_tuple[0],  COLUMN_AUTHOR, book_tuple[1],  COLUMN_PATH, \
                            book_tuple[2].rstrip())
        f.close()
        self.list_scroller.show()
        self.list_scroller_visible = True
     
    def get_book(self):
        self.progressbar.show()
        self.books_toolbar.enable_button(False)
        self.list_scroller.props.sensitive = False
        if self.selected_path.startswith('PGA'):
            gobject.idle_add(self.download_book,  self.selected_path.replace('PGA', 'http://gutenberg.net.au'),  \
                             self.get_book_result_cb)
        elif self.selected_path.startswith('/etext'):
            gobject.idle_add(self.download_book,  "http://www.gutenberg.org/dirs" + self.selected_path + "108.zip",  \
                             self.get_old_book_result_cb)
        else:
            gobject.idle_add(self.download_book,  "http://www.gutenberg.org/dirs" + self.selected_path + "-8.zip",  \
                             self.get_iso_book_result_cb)
        
    def download_book(self,  url,  result_cb):
        path = os.path.join(self.get_activity_root(), 'instance',
                            'tmp%i' % time.time())
        getter = ReadURLDownloader(url)
        getter.connect("finished", result_cb)
        getter.connect("progress", self.get_book_progress_cb)
        getter.connect("error", self.get_book_error_cb)
        logger.debug("Starting download to %s...", path)
        try:
            getter.start(path)
        except:
            self.alert(_('Error'), _('Connection timed out for ') + self.selected_title)
           
        self.download_content_length = getter.get_content_length()
        self.download_content_type = getter.get_content_type()
        self.textview.grab_focus()

    def get_iso_book_result_cb(self, getter, tempfile, suggested_name):
        if self.download_content_type.startswith('text/html'):
            # got an error page instead
            self.download_book("http://www.gutenberg.org/dirs" + self.selected_path + ".zip",  self.get_book_result_cb)
            return
        self.process_downloaded_book(tempfile,  suggested_name)

    def get_old_book_result_cb(self, getter, tempfile, suggested_name):
        if self.download_content_type.startswith('text/html'):
            # got an error page instead
            self.download_book("http://www.gutenberg.org/dirs" + self.selected_path + "10.zip",  self.get_book_result_cb)
            return
        self.process_downloaded_book(tempfile,  suggested_name)

    def get_book_result_cb(self, getter, tempfile, suggested_name):
        if self.download_content_type.startswith('text/html'):
            # got an error page instead
            self.get_book_error_cb(getter, 'HTTP Error')
            return
        self.process_downloaded_book(tempfile,  suggested_name)

    def get_book_progress_cb(self, getter, bytes_downloaded):
        if self.download_content_length > 0:
            logger.debug("Downloaded %u of %u bytes...",
                          bytes_downloaded, self.download_content_length)
        else:
            logger.debug("Downloaded %u bytes...",
                          bytes_downloaded)
        total = self.download_content_length
        self.set_downloaded_bytes(bytes_downloaded,  total)
        gtk.gdk.threads_enter()
        while gtk.events_pending():
            gtk.main_iteration()
        gtk.gdk.threads_leave()

    def set_downloaded_bytes(self, bytes,  total):
        fraction = float(bytes) / float(total)
        self.progressbar.set_fraction(fraction)
        
    def clear_downloaded_bytes(self):
        self.progressbar.set_fraction(0.0)

    def get_book_error_cb(self, getter, err):
        self.list_scroller.props.sensitive = True
        self.progressbar.hide()
        logger.debug("Error getting document: %s", err)
        self.alert(_('Error'), _('Could not download ') + self.selected_title + _(' path in catalog may be incorrect.'))
        self.download_content_length = 0
        self.download_content_type = None

    def process_downloaded_book(self,  tempfile,  suggested_name):
        self.list_scroller.props.sensitive = True
        self.tempfile = tempfile
        file_path = os.path.join(self.get_activity_root(), 'instance',
                                    '%i' % time.time())
        os.link(tempfile, file_path)
        logger.debug("Got document %s (%s)", tempfile, suggested_name)
        self.create_journal_entry(tempfile)
        self.load_document(tempfile)

    def create_journal_entry(self,  tempfile):
        self.progressbar.hide()
        journal_entry = datastore.create()
        journal_title = self.selected_title
        if self.selected_author != ' ':
            journal_title = journal_title  + ', by ' + self.selected_author
        journal_entry.metadata['title'] = journal_title
        self.metadata['title'] = journal_title
        journal_entry.metadata['title_set_by_user'] = '1'
        journal_entry.metadata['activity'] = self.get_bundle_id()
        journal_entry.metadata['keep'] = '0'
        journal_entry.metadata['mime_type'] = 'application/zip'
        journal_entry.metadata['buddies'] = ''
        journal_entry.metadata['preview'] = ''
        journal_entry.metadata['icon-color'] = profile.get_color().to_string()
        journal_entry.metadata['tags'] = self.selected_author
        journal_entry.file_path = tempfile
        datastore.write(journal_entry)
        self.extra_journal_entry = journal_entry
        self.alert(_('Success'), self.selected_title + _(' added to Journal.'))

    def find_previous(self):
        self.current_found_item = self.current_found_item - 1
        if self.current_found_item <= 0:
            self.current_found_item = 0
        current_found_tuple = self.found_records[self.current_found_item]
        self.page = current_found_tuple[0]
        self.read_toolbar.set_current_page(self.page)
        self.show_found_page(current_found_tuple)

    def find_next(self):
        self.current_found_item = self.current_found_item + 1
        if self.current_found_item >= len(self.found_records):
            self.current_found_item = len(self.found_records) - 1
        current_found_tuple = self.found_records[self.current_found_item]
        self.page = current_found_tuple[0]
        self.read_toolbar.set_current_page(self.page)
        self.show_found_page(current_found_tuple)
    
    def can_find_previous(self):
        if self.current_found_item == 0:
            return False
        return True
    
    def can_find_next(self):
        if self.current_found_item >= len(self.found_records) - 1:
            return False
        return True
    
    def find_begin(self, search_text):
        pagecount = 0
        linecount = 0
        charcount = 0
        self.found_records = []
        self.current_found_item = -1
        self.etext_file.seek(0)
        while self.etext_file:
            line = unicode(self.etext_file.readline(), "iso-8859-1")
            line_length = len(line)
            if not line:
                break
            linecount = linecount + 1
            positions = self.allindices(line.lower(), search_text.lower())
            for position in positions:
                found_pos = charcount + position + 3
                found_tuple = (pagecount, found_pos, len(search_text) + found_pos)
                self.found_records.append(found_tuple)
                self.current_found_item = 0
            charcount = charcount + line_length  
            if linecount >= PAGE_SIZE:
                linecount = 0
                charcount = 0
                pagecount = pagecount + 1
        if self.current_found_item == 0:
            current_found_tuple = self.found_records[self.current_found_item]
            self.page = current_found_tuple[0]
            self.read_toolbar.set_current_page(self.page)
            self.show_found_page(current_found_tuple)

    def allindices(self,  line, search, listindex=None,  offset=0):
        if listindex is None:   
            listindex = [] 
        if (line.find(search) == -1):
            return listindex 
        else: 
            offset = line.index(search)+offset 
            listindex.append(offset) 
            line = line[(line.index(search)+1):] 
            return self.allindices(line, search, listindex, offset+1)
    
    def get_current_page(self):
        return self.page

    # The code from here on down is for sharing.
    def download_result_cb(self, getter, tempfile, suggested_name, tube_id):
        if self.download_content_type.startswith('text/html'):
            # got an error page instead
            self.download_error_cb(getter, 'HTTP Error', tube_id)
            return

        del self.unused_download_tubes

        self.tempfile = tempfile
        file_path = os.path.join(self.get_activity_root(), 'instance',
                                    '%i' % time.time())
        logger.debug("Saving file %s to datastore...", file_path)
        os.link(tempfile, file_path)
        self._jobject.file_path = file_path
        datastore.write(self._jobject, transfer_ownership=True)

        logger.debug("Got document %s (%s) from tube %u",
                      tempfile, suggested_name, tube_id)
        self.load_document(tempfile)
        self.save()
        self.progressbar.hide()

    def download_progress_cb(self, getter, bytes_downloaded, tube_id):
        if self.download_content_length > 0:
            logger.debug("Downloaded %u of %u bytes from tube %u...",
                          bytes_downloaded, self.download_content_length, 
                          tube_id)
        else:
            logger.debug("Downloaded %u bytes from tube %u...",
                          bytes_downloaded, tube_id)
        total = self.download_content_length
        self.set_downloaded_bytes(bytes_downloaded,  total)
        gtk.gdk.threads_enter()
        while gtk.events_pending():
            gtk.main_iteration()
        gtk.gdk.threads_leave()

    def download_error_cb(self, getter, err, tube_id):
        self.progressbar.hide()
        logger.debug("Error getting document from tube %u: %s",
                      tube_id, err)
        self.alert(_('Failure'), _('Error getting document from tube'))
        self.want_document = True
        self.download_content_length = 0
        self.download_content_type = None
        gobject.idle_add(self.get_document)

    def download_document(self, tube_id, path):
        # FIXME: should ideally have the CM listen on a Unix socket
        # instead of IPv4 (might be more compatible with Rainbow)
        chan = self._shared_activity.telepathy_tubes_chan
        iface = chan[telepathy.CHANNEL_TYPE_TUBES]
        addr = iface.AcceptStreamTube(tube_id,
                telepathy.SOCKET_ADDRESS_TYPE_IPV4,
                telepathy.SOCKET_ACCESS_CONTROL_LOCALHOST, 0,
                utf8_strings=True)
        logger.debug('Accepted stream tube: listening address is %r', addr)
        # SOCKET_ADDRESS_TYPE_IPV4 is defined to have addresses of type '(sq)'
        assert isinstance(addr, dbus.Struct)
        assert len(addr) == 2
        assert isinstance(addr[0], str)
        assert isinstance(addr[1], (int, long))
        assert addr[1] > 0 and addr[1] < 65536
        port = int(addr[1])

        self.progressbar.show()
        getter = ReadURLDownloader("http://%s:%d/document"
                                           % (addr[0], port))
        getter.connect("finished", self.download_result_cb, tube_id)
        getter.connect("progress", self.download_progress_cb, tube_id)
        getter.connect("error", self.download_error_cb, tube_id)
        logger.debug("Starting download to %s...", path)
        getter.start(path)
        self.download_content_length = getter.get_content_length()
        self.download_content_type = getter.get_content_type()
        return False

    def get_document(self):
        if not self.want_document:
            return False

        # Assign a file path to download if one doesn't exist yet
        if not self._jobject.file_path:
            path = os.path.join(self.get_activity_root(), 'instance',
                                'tmp%i' % time.time())
        else:
            path = self._jobject.file_path

        # Pick an arbitrary tube we can try to download the document from
        try:
            tube_id = self.unused_download_tubes.pop()
        except (ValueError, KeyError), e:
            logger.debug('No tubes to get the document from right now: %s',
                          e)
            return False

        # Avoid trying to download the document multiple times at once
        self.want_document = False
        gobject.idle_add(self.download_document, tube_id, path)
        return False

    def joined_cb(self, also_self):
        """Callback for when a shared activity is joined.

        Get the shared document from another participant.
        """
        self.watch_for_tubes()
        gobject.idle_add(self.get_document)

    def share_document(self):
        """Share the document."""
        # FIXME: should ideally have the fileserver listen on a Unix socket
        # instead of IPv4 (might be more compatible with Rainbow)

        logger.debug('Starting HTTP server on port %d', self.port)
        self.fileserver = ReadHTTPServer(("", self.port),
            self.tempfile)

        # Make a tube for it
        chan = self._shared_activity.telepathy_tubes_chan
        iface = chan[telepathy.CHANNEL_TYPE_TUBES]
        self.fileserver_tube_id = iface.OfferStreamTube(READ_STREAM_SERVICE,
                {},
                telepathy.SOCKET_ADDRESS_TYPE_IPV4,
                ('127.0.0.1', dbus.UInt16(self.port)),
                telepathy.SOCKET_ACCESS_CONTROL_LOCALHOST, 0)

    def watch_for_tubes(self):
        """Watch for new tubes."""
        tubes_chan = self._shared_activity.telepathy_tubes_chan

        tubes_chan[telepathy.CHANNEL_TYPE_TUBES].connect_to_signal('NewTube',
            self.new_tube_cb)
        tubes_chan[telepathy.CHANNEL_TYPE_TUBES].ListTubes(
            reply_handler=self.list_tubes_reply_cb,
            error_handler=self.list_tubes_error_cb)

    def new_tube_cb(self, tube_id, initiator, tube_type, service, params,
                     state):
        """Callback when a new tube becomes available."""
        logger.debug('New tube: ID=%d initator=%d type=%d service=%s '
                      'params=%r state=%d', tube_id, initiator, tube_type,
                      service, params, state)
        if service == READ_STREAM_SERVICE:
            logger.debug('I could download from that tube')
            self.unused_download_tubes.add(tube_id)
            # if no download is in progress, let's fetch the document
            if self.want_document:
                gobject.idle_add(self.get_document)

    def list_tubes_reply_cb(self, tubes):
        """Callback when new tubes are available."""
        for tube_info in tubes:
            self.new_tube_cb(*tube_info)

    def list_tubes_error_cb(self, e):
        """Handle ListTubes error by logging."""
        logger.error('ListTubes() failed: %s', e)
 
    def shared_cb(self, activityid):
        """Callback when activity shared.

        Set up to share the document.

        """
        # We initiated this activity and have now shared it, so by
        # definition we have the file.
        logger.debug('Activity became shared')
        self.watch_for_tubes()
        self.share_document()

    def alert(self, title, text=None):
        alert = NotifyAlert(timeout=20)
        alert.props.title = title
        alert.props.msg = text
        self.add_alert(alert)
        alert.connect('response', self.alert_cancel_cb)
        alert.show()

    def alert_cancel_cb(self, alert, response_id):
        self.remove_alert(alert)
        self.textview.grab_focus()

    # From here down is power management stuff.

    def now_active_cb(self, widget, pspec):
        if self.props.active:
            # Now active, start initial suspend timeout
            xopower.reset_sleep_timer()
            xopower.sleep_inhibit = False
        else:
            # Now inactive
            xopower.sleep_inhibit = True

    def focus_in_event_cb(self, widget, event):
        xopower.turn_on_sleep_timer()

    def focus_out_event_cb(self, widget, event):
        xopower.turn_off_sleep_timer()

    def user_action_cb(self, widget):
        xopower.reset_sleep_timer()

    def suspend_cb(self):
        xopower.suspend()
        return False
 
