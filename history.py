import os
import glob
import platform
import time
from datetime import datetime as dt
import difflib
import filecmp
import shutil
import subprocess
import sublime
import sublime_plugin

#==============#
#   Messages   #
#==============#
NO_HISTORY_MSG = 'No local history found'
NO_INCREMENTAL_DIFF = 'No incremental diff found'
HISTORY_DELETED_MSG = 'All local history deleted'

S = None


def plugin_loaded():
    global S
    S = sublime.load_settings('LocalHistory.sublime-settings')


def get_history_path():
    default_history_path = os.path.join(
        os.path.abspath(os.path.expanduser('~')), '.sublime', 'history')
    return S.get("history_path", default_history_path)


def get_file_dir(file_path, history_path=None):
    if history_path is None:
        history_path = get_history_path()
    file_dir = os.path.dirname(file_path)
    if platform.system() == 'Windows':
        if file_dir.find(os.sep) == 0:
            file_dir = file_dir[2:]  # Strip the network \\ starting path
        if file_dir.find(':') == 1:
            file_dir = file_dir.replace(':', '', 1)
    else:
        file_dir = file_dir[1:]  # Trim the root
    return os.path.join(history_path, file_dir)


def get_pretty_printed_file_times(file_list):
    return [dt.fromtimestamp(os.path.getmtime(f)).strftime('%m/%d/%Y, %I:%M:%S %p') for f in file_list]


def get_diff(from_file, to_file):
    # From
    with open(from_file, 'r', encoding='utf-8') as f:
        from_content = f.readlines()

    # To
    with open(to_file, 'r', encoding='utf-8') as f:
        to_content = f.readlines()

    # Compare and show diff
    diff = difflib.unified_diff(from_content, to_content, from_file, to_file)
    diff = ''.join(diff)
    return diff


def get_new_diff_view():
    view = sublime.active_window().new_file()
    view.set_scratch(True)
    view.set_syntax_file('Packages/Diff/Diff.tmLanguage')
    view.set_name('Diff View')
    return view


class HistorySave(sublime_plugin.EventListener):

    def on_pre_save(self, view):
        if not os.path.exists(view.file_name()):
            return

        self.process_history(view.file_name(),
                             get_history_path(),
                             S.get('file_size_limit'),
                             S.get('history_retention'))

    def on_post_save(self, view):
        self.process_history(view.file_name(),
                             get_history_path(),
                             S.get('file_size_limit'),
                             S.get('history_retention'))

    def process_history(self, file_path, history_path, file_size_limit, history_retention):
        # Return if file exceeds the size limit
        if os.path.getsize(file_path) > file_size_limit:
            print('WARNING: Local History did not save a copy of this file \
                   because it has exceeded {0}KB limit.'.format(file_size_limit / 1024))
            return

        # Get history directory
        file_name = os.path.basename(file_path)
        history_dir = get_file_dir(file_path, history_path)
        if not os.path.exists(history_dir):
            # Create directory structure
            os.makedirs(history_dir)

        # Get history files
        history_files = glob.glob(os.path.join(history_dir, '*' + file_name))
        history_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)

        # Skip if no changes
        if history_files:
            if filecmp.cmp(file_path, history_files[0]):
                return

        # Store history
        new_file_name = '{0}.{1}'.format(dt.fromtimestamp(os.path.getmtime(file_path)).strftime('%Y-%m-%d_%H.%M.%S'), file_name)
        new_file_path = os.path.join(history_dir, new_file_name)
        shutil.copyfile(file_path, new_file_path)

        # Set timestamps on the history file
        file_stat = os.stat(file_path)
        os.utime(new_file_path, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))

        # Remove old files
        now = time.time()
        for file in history_files:
            # convert to seconds
            if os.path.getmtime(file) < now - history_retention * 86400:
                os.remove(file)


class HistoryBrowse(sublime_plugin.TextCommand):

    def run(self, edit):
        system = platform.system()
        if system == 'Darwin':
            subprocess.call(['open', get_file_dir(self.view.file_name())])
        elif system == 'Linux':
            subprocess.call(['xdg-open', get_file_dir(self.view.file_name())])
        elif system == 'Windows':
            subprocess.call(['explorer', get_file_dir(self.view.file_name())])


class HistoryOpen(sublime_plugin.TextCommand):

    def run(self, edit):
        # Get history directory
        file_name = os.path.basename(self.view.file_name())
        history_dir = get_file_dir(self.view.file_name())

        # Get history files
        if not os.path.isdir(history_dir):
            sublime.status_message(NO_HISTORY_MSG)
            return

        history_files = glob.glob(os.path.join(history_dir, '*' + file_name))
        history_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        if not history_files:
            sublime.status_message(NO_HISTORY_MSG)
            return

        panel_list = get_pretty_printed_file_times(history_files)

        diff_view = get_new_diff_view()

        def on_done(index):
            self.view.window().run_command('close_file')

            # Escape
            if index == -1:
                return

            # Sublime Text 3 has a bug wherein calling open_file from within a panel
            # callback causes the new view to not have focus. Make a deferred call via
            # set_timeout to workaround this issue.
            sublime.set_timeout(lambda: self.view.window().open_file(history_files[index]), 0)

        def on_highlight(index):
            if self.view.is_dirty():
                self.view.run_command('save')

            from_file = history_files[index]
            to_file = self.view.file_name()
            diff_view.run_command('show_diff', {'from_file': from_file, 'to_file': to_file})

        self.view.window().show_quick_panel(panel_list, on_done, on_highlight=on_highlight)


class HistoryIncrementalDiff(sublime_plugin.TextCommand):

    def run(self, edit):
        # Get history directory
        file_name = os.path.basename(self.view.file_name())
        history_dir = get_file_dir(self.view.file_name())

        # Get history files
        history_files = glob.glob(os.path.join(history_dir, '*' + file_name))
        history_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        if len(history_files) < 2:
            sublime.status_message(NO_INCREMENTAL_DIFF)
            return

        panel_list = get_pretty_printed_file_times(history_files)

        # Remove the last item in the panel list because there is nothing to compare it to.
        # Note that history_list is not changed, because we still need the last file in it, so that
        # the previous entry can be compared with it.
        panel_list.pop()

        diff_view = get_new_diff_view()

        def on_done(index):
            # Escape
            if index == -1:
                self.view.window().run_command('close_file')
                return

            # Diff view is already open. Nothing to do here.

        def on_highlight(index):
            from_file = history_files[index + 1]
            to_file = history_files[index]
            diff_view.run_command('show_diff', {'from_file': from_file, 'to_file': to_file})

        self.view.window().show_quick_panel(panel_list, on_done, on_highlight=on_highlight)


class ShowDiff(sublime_plugin.TextCommand):

    def run(self, edit, **kwargs):
        from_file = kwargs['from_file']
        to_file = kwargs['to_file']
        diff = get_diff(from_file, to_file)
        view = self.view
        view.erase(edit, sublime.Region(0, view.size()))
        if diff:
            view.insert(edit, 0, diff)
        else:
            view.insert(edit, 0, 'No differences.')


class HistoryDeleteAll(sublime_plugin.TextCommand):

    def run(self, edit):
        history_path = get_history_path()
        if os.path.exists(history_path):
            shutil.rmtree(history_path)
        sublime.status_message(HISTORY_DELETED_MSG)
