import tomllib


__LOADED_CONFIG = None


def get_config():
    global __LOADED_CONFIG
    if __LOADED_CONFIG is not None:
        return __LOADED_CONFIG

    try:
        with open("bot_config.toml", "rb") as f:
            __LOADED_CONFIG = tomllib.load(f)
            return __LOADED_CONFIG
    except Exception:
        __LOADED_CONFIG = dict()
        return __LOADED_CONFIG
