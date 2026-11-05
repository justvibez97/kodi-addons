"""Minimal stand-in for Kodi's xbmc module (headless tests only)."""
import time
LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = 0, 1, 2, 3
LOG = []
BUILTINS = []
VERBOSE = False


def log(msg, level=LOGINFO):
    LOG.append(msg)
    if VERBOSE or level >= LOGINFO:
        print('    [kodi log]', msg)


def sleep(ms):
    time.sleep(ms / 1000.0)


def getCondVisibility(cond):
    return False


def getInfoLabel(label):
    return ''


def executebuiltin(cmd, wait=False):
    BUILTINS.append(cmd)


class Monitor(object):
    def abortRequested(self):
        return False

    def waitForAbort(self, t=0):
        time.sleep(t)
        return False


class Player(object):
    def isPlaying(self):
        return False

    def isPlayingVideo(self):
        return False

    def play(self, *a, **k):
        pass
