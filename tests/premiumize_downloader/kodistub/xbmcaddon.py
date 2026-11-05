import os
SETTINGS = {}
PROFILE = os.environ.get('PMDL_TEST_PROFILE', '.')


class Addon(object):
    def __init__(self, addon_id=None):
        pass

    def getSetting(self, key):
        return str(SETTINGS.get(key, ''))

    def getSettingBool(self, key):
        return str(SETTINGS.get(key, 'false')).lower() == 'true'

    def getSettingInt(self, key):
        return int(SETTINGS.get(key, 0))

    def setSetting(self, key, value):
        SETTINGS[key] = value

    def getAddonInfo(self, key):
        return {'profile': PROFILE, 'path': os.path.dirname(os.path.abspath(__file__)), 'id': 'test'}.get(key, '')

    def openSettings(self):
        pass
