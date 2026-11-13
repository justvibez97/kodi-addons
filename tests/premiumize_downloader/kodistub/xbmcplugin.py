SORT_METHOD_UNSORTED = SORT_METHOD_LABEL_IGNORE_FOLDERS = SORT_METHOD_LABEL = 0
ITEMS = []          # (url, ListItem, is_folder) added by the plugin; tests clear it
RESOLVED = []       # (succeeded, ListItem) from setResolvedUrl


def addDirectoryItem(handle, url, listitem, isFolder=False, totalItems=0):
    ITEMS.append((url, listitem, isFolder))
    return True


def endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=True):
    pass


def setContent(handle, content):
    pass


def addSortMethod(handle, method, *a, **k):
    pass


def setResolvedUrl(handle, succeeded, listitem):
    RESOLVED.append((succeeded, listitem))
