from ConfigParser import ConfigParser, Error as ConfigKeyError
import os

_config = None


def get_config():
    global _config
    if not _config:
        _config = OmsConfig()
    return _config


class OmsConfig(ConfigParser):
    __default_files__ = ['/usr/lib/opennode/opennode-oms-defaults.conf', './opennode-oms.conf', '/etc/opennode/opennode-oms.conf', '~/.opennode-oms.conf']

    def __init__(self, config_filenames=__default_files__):
        ConfigParser.__init__(self)
        self.read([os.path.expanduser(i) for i in config_filenames])

    def getboolean(self, section, option, default=False):
        try:
            return ConfigParser.getboolean(self, section, option)
        except ConfigKeyError:
            return default

    def getint(self, section, option, default=False):
        try:
            return ConfigParser.getint(self, section, option)
        except ConfigKeyError:
            return default

    def getfloat(self, section, option, default=False):
        try:
            return ConfigParser.getfloat(self, section, option)
        except ConfigKeyError:
            return default
