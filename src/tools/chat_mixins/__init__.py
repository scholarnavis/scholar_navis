"""Chat mixins split from src/tools/chat_tool.py by functional domain.

- ChatSendFlowMixin: query dispatch & AI response launch
- ChatResponseFlowMixin: streaming render & finish/error handling
- ChatBubblesMixin: bubble creation, scrolling & follow-ups
- ChatSearchMixin: in-conversation search (SearchBar based)
- ChatBookmarksMixin: bookmarks, scrollbar marks & bookmark set
- ChatAttachmentsMixin: attachments & history export
- ChatSessionMixin: session autosave & startup restore
"""
from src.tools.chat_mixins.send_flow import ChatSendFlowMixin
from src.tools.chat_mixins.response_flow import ChatResponseFlowMixin
from src.tools.chat_mixins.bubbles import ChatBubblesMixin
from src.tools.chat_mixins.search import ChatSearchMixin
from src.tools.chat_mixins.bookmarks import ChatBookmarksMixin
from src.tools.chat_mixins.attachments import ChatAttachmentsMixin
from src.tools.chat_mixins.session import ChatSessionMixin

__all__ = [
    "ChatSendFlowMixin",
    "ChatResponseFlowMixin",
    "ChatBubblesMixin",
    "ChatSearchMixin",
    "ChatBookmarksMixin",
    "ChatAttachmentsMixin",
    "ChatSessionMixin",
]
