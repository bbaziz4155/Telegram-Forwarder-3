from telegram.ext import ConversationHandler

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
