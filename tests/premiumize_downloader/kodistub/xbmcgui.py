NOTES = []
NOTIFICATION_INFO, NOTIFICATION_WARNING, NOTIFICATION_ERROR = 'info', 'warning', 'error'
_PROPS = {}


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
        return True


class ListItem(object):
    def __init__(self, label='', label2='', path='', offscreen=False):
        self.label, self.path = label, path


class DialogProgressBG(object):
    def create(self, *a):
        pass

    def update(self, *a):
        pass

    def close(self):
        pass
