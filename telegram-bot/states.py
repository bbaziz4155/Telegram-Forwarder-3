# Main menu states
(
    MAIN_MENU,
    ADD_RULE_SOURCE,
    ADD_RULE_DEST,
    ADD_RULE_CONFIRM,
    DELETE_RULE_SELECT,
    IGNORE_ADD_CHAT,
    IGNORE_REMOVE_SELECT,
    FORWARD_HISTORY_SOURCE,
    FORWARD_HISTORY_DEST,
    FORWARD_HISTORY_LIMIT,
) = range(10)

# Copy / dryrun / sync wizard states (used by handlers/copybot.py)
COPY_AWAIT_SRC     = 10
COPY_AWAIT_DST     = 11
COPY_OPTIONS       = 12
COPY_AWAIT_REPLACE = 13

# In-bot userbot login wizard states (used by handlers/login.py)
LOGIN_PHONE = 14
LOGIN_OTP   = 15
LOGIN_2FA   = 16

# Caption-preview wizard state (used by handlers/preview.py)
PREVIEW_AWAIT_MSG = 17

# /gensession wizard states (used by handlers/gensession.py)
GENSESSION_PHONE = 18
GENSESSION_OTP   = 19
GENSESSION_2FA   = 20

# Admin management states (used by handlers/admin_mgmt.py)
ADMIN_MGMT     = 21
ADMIN_AWAIT_ID = 22

# Strip-pattern management states (used by handlers/strippatterns.py)
STRIP_MGMT      = 23
STRIP_AWAIT_ADD  = 24
STRIP_AWAIT_TEST = 25
