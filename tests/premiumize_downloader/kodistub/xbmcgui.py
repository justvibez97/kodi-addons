NOTES = []
NOTIFICATION_INFO, NOTIFICATION_WARNING, NOTIFICATION_ERROR = 'info', 'warning', 'error'
_PROPS = {}
YESNO, SELECT, INPUT = [], [], []     # queued answers for dialogs (tests)
ASKED = []                          # (heading, text) of every yes/no dialog shown


class Window(object):
    def __init__(self, wid=None):
        pass

    def getProperty(self, k):
        return _PROPS.get(k, '')

    def setProperty(self, k, v):
        _PROPS[k] = v

    def clearProperty(self, k):
        _PROPS.pop(k, None)


class Dialog(object):
    def notification(self, heading, message, icon=None, time=0, sound=True):
        NOTES.append((heading, message))
        print('    [notification]', heading, '|', message)

    def ok(self, heading, message):
        print('    [ok dialog]', heading, '|', message)
        return True

    def yesno(self, *a, **k):
        print('    [yes/no dialog]', a[:2])
        ASKED.append(a[:2])
        return YESNO.pop(0) if YESNO else True

    def select(self, heading, options, *a, **k):
        print('    [select dialog]', heading, '|', options)
        return SELECT.pop(0) if SELECT else -1

    def input(self, *a, **k):
        return INPUT.pop(0) if INPUT else ''

    def browse(self, *a, **k):
        return ''


class _Tag(object):
    def __getattr__(self, name):
        return lambda *a, **k: None


class ListItem(object):
    def __init__(self, label='', label2='', path='', offscreen=False):
        self.label, self.label2, self.path = label, label2, path
        self.context, self.props, self.subs = [], {}, []

    def getLabel(self):
        return self.label

    def setArt(self, art):
        pass

    def addContextMenuItems(self, items):
        self.context += list(items)

    def setProperty(self, k, v):
        self.props[k] = v

    def getVideoInfoTag(self):
        return _Tag()

    def setInfo(self, *a):
        pass

    def setMimeType(self, m):
        pass

    def setContentLookup(self, v):
        pass

    def setSubtitles(self, subs):
        self.subs = list(subs)


class DialogProgressBG(object):
    def create(self, *a):
        pass

    def update(self, *a):
        pass

    def close(self):
        pass


class DialogProgress(object):
    def create(self, *a):
        pass

    def update(self, *a):
        pass

    def iscanceled(self):
        return False

    def close(self):
        pass
