"""Fresh action ids and nonces; the randomness the domain is not allowed to touch."""

from __future__ import annotations

import secrets
import uuid

from archshell.domain.workflow import FreshIds


def fresh_ids() -> FreshIds:
    return FreshIds(action_id=str(uuid.uuid4()), nonce=secrets.token_urlsafe(16))
