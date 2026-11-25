"""Permissions and query conditions for Broadcast and Broadcast Recipient (P1).

Implements granular per-channel permission logic, read-all role evaluation,
query conditions for SQL filtering in get_list, and row-level checks.
"""

import frappe

from msuite_campaigns.broadcast.constants import (
	BROADCAST_ADMIN_ROLES,
	BROADCAST_ALL_CHANNEL_ROLES,
	BROADCAST_READ_ALL_ROLES,
	CHANNEL_MANAGER_ROLES,
	CHANNEL_ROLES,
	CHANNELS,
)


def _get_doc_channels(doc) -> list[str]:
	"""Extract list of channel names from a Broadcast document or dict.

	Params:
		doc: Broadcast Document, dict, or docname str.

	Returns:
		list[str]: Distinct list of channel names.

	Exceptions:
		None.

	Side effects:
		May query Broadcast Channel table if doc is passed as docname.
	"""
	if not doc:
		return []
	if isinstance(doc, str):
		if frappe.db.table_exists("Broadcast Channel"):
			return list(frappe.get_all("Broadcast Channel", filters={"parent": doc}, pluck="channel"))
		return []

	raw_channels = getattr(doc, "channels", None)
	if raw_channels is None and isinstance(doc, dict):
		raw_channels = doc.get("channels")
	if raw_channels is not None:
		channels: list[str] = []
		for r in raw_channels:
			ch = getattr(r, "channel", None) or (r.get("channel") if isinstance(r, dict) else None)
			if ch and ch not in channels:
				channels.append(ch)
		return channels

	doc_name = getattr(doc, "name", None) or (doc.get("name") if isinstance(doc, dict) else None)
	if doc_name and frappe.db.table_exists("Broadcast Channel") and frappe.db.exists("Broadcast", doc_name):
		return list(frappe.get_all("Broadcast Channel", filters={"parent": doc_name}, pluck="channel"))

	return []


def _get_recipient_channel(doc) -> str | None:
	"""Extract channel name from a Broadcast Recipient document or dict.

	Params:
		doc: Broadcast Recipient Document, dict, or docname str.

	Returns:
		str | None: Channel name or None if not present.

	Exceptions:
		None.

	Side effects:
		May query Broadcast Recipient table if doc is passed as docname.
	"""
	if not doc:
		return None
	if isinstance(doc, str):
		if frappe.db.table_exists("Broadcast Recipient"):
			return frappe.db.get_value("Broadcast Recipient", doc, "channel")
		return None

	ch = getattr(doc, "channel", None)
	if ch is None and isinstance(doc, dict):
		ch = doc.get("channel")
	return ch


def permitted_channels(user: str | None = None) -> set[str]:
	"""Return set of channels whose role the user holds; admin/read-all/all-channel roles return all CHANNELS.

	Guest never has permitted channels (returns empty set).
	Campaign User holds ALL channels for read/write/create.

	Params:
		user: User email or None (defaults to frappe.session.user).

	Returns:
		set[str]: Set of permitted channel names.

	Exceptions:
		None.

	Side effects:
		None.
	"""
	if not user:
		user = frappe.session.user
	if not user or user == "Guest":
		return set()

	user_roles = set(frappe.get_roles(user))
	if (
		user == "Administrator"
		or any(r in user_roles for r in BROADCAST_READ_ALL_ROLES)
		or any(r in user_roles for r in BROADCAST_ALL_CHANNEL_ROLES)
	):
		return set(CHANNELS)

	permitted = set()
	for ch, roles in CHANNEL_ROLES.items():
		if any(r in user_roles for r in roles):
			permitted.add(ch)
	return permitted


def has_permission(doc, ptype: str = "read", user: str | None = None, **kwargs) -> bool:
	"""Evaluate P1 permission rules for Broadcast.

	Read requires read-all/all-channel role OR user holds the role of at least one channel row.
	Create/write requires admin role, all-channel role (e.g. Campaign User), OR role for EVERY channel row of doc.
	Delete requires admin role (BROADCAST_ADMIN_ROLES) OR channel Manager role for EVERY channel row of doc.
	(a doc with no channel rows yet allows create/write for anyone with any channel role, all-channel role, or admin).
	Guest never has permission.

	Params:
		doc: Broadcast Document, dict, or docname.
		ptype: Permission type ('read', 'write', 'create', 'delete', 'select', etc.).
		user: User email or None (defaults to frappe.session.user).
		**kwargs: Additional arguments passed by Frappe hook.

	Returns:
		bool: True if permitted, False otherwise.

	Exceptions:
		None.

	Side effects:
		May query DB to inspect channel rows if doc is docname.
	"""
	if not user:
		user = frappe.session.user
	if not user or user == "Guest":
		return False

	user_roles = set(frappe.get_roles(user))
	is_admin = (user == "Administrator") or any(r in user_roles for r in BROADCAST_ADMIN_ROLES)
	if is_admin:
		return True

	doc_channels = _get_doc_channels(doc)

	if ptype == "delete":
		# Delete strictly requires BROADCAST_ADMIN_ROLES or channel Manager role for every channel on doc.
		# Campaign User and regular channel Users cannot delete.
		held_manager_channels = {ch for ch, mgr in CHANNEL_MANAGER_ROLES.items() if mgr in user_roles}
		if not doc_channels:
			return bool(held_manager_channels)
		return all(ch in held_manager_channels for ch in doc_channels)

	is_read_all = any(r in user_roles for r in BROADCAST_READ_ALL_ROLES)
	is_all_channel = any(r in user_roles for r in BROADCAST_ALL_CHANNEL_ROLES)
	held_channels = {ch for ch, roles in CHANNEL_ROLES.items() if any(r in user_roles for r in roles)}
	if is_all_channel:
		held_channels = set(CHANNELS)

	if ptype in ("read", "select"):
		if is_read_all or is_all_channel:
			return True
		if not doc_channels:
			return bool(held_channels)
		return any(ch in held_channels for ch in doc_channels)

	# create, write, submit, cancel, etc.
	# Admin already returned True above; non-admin with read-only roles (like Viewer) denied here
	if not doc_channels:
		# doc with no channel rows yet: allow create for anyone with any channel role or admin
		return bool(held_channels)

	return all(ch in held_channels for ch in doc_channels)


def recipient_has_permission(doc, ptype: str = "read", user: str | None = None, **kwargs) -> bool:
	"""Evaluate permission rules for Broadcast Recipient filtered on channel column.

	Read requires read-all/all-channel role OR user holds the role of the recipient's channel.
	Write/create requires admin role, all-channel role, OR role of the recipient's channel.
	Delete requires admin role OR channel Manager role of the recipient's channel.
	Guest never has permission.

	Params:
		doc: Broadcast Recipient Document, dict, or docname.
		ptype: Permission type ('read', 'write', 'create', 'delete', 'select', etc.).
		user: User email or None (defaults to frappe.session.user).
		**kwargs: Additional arguments passed by Frappe hook.

	Returns:
		bool: True if permitted, False otherwise.

	Exceptions:
		None.

	Side effects:
		May query DB for recipient channel if doc is docname.
	"""
	if not user:
		user = frappe.session.user
	if not user or user == "Guest":
		return False

	user_roles = set(frappe.get_roles(user))
	is_admin = (user == "Administrator") or any(r in user_roles for r in BROADCAST_ADMIN_ROLES)
	if is_admin:
		return True

	channel = _get_recipient_channel(doc)

	if ptype == "delete":
		held_manager_channels = {ch for ch, mgr in CHANNEL_MANAGER_ROLES.items() if mgr in user_roles}
		if not channel:
			return bool(held_manager_channels)
		return channel in held_manager_channels

	is_read_all = any(r in user_roles for r in BROADCAST_READ_ALL_ROLES)
	is_all_channel = any(r in user_roles for r in BROADCAST_ALL_CHANNEL_ROLES)
	held_channels = {ch for ch, roles in CHANNEL_ROLES.items() if any(r in user_roles for r in roles)}
	if is_all_channel:
		held_channels = set(CHANNELS)

	if ptype in ("read", "select"):
		if is_read_all or is_all_channel:
			return True
		if not channel:
			return bool(held_channels)
		return channel in held_channels

	# write, create
	if not channel:
		return bool(held_channels)
	return channel in held_channels


def broadcast_query_conditions(user: str | None = None) -> str:
	"""Return permission_query_conditions SQL for Broadcast.

	Returns empty string for read-all roles, all-channel roles, or Administrator.
	Returns exists clause matching permitted channels for channel role holders.
	Returns '1=0' for users with no channel role and Guest.

	Params:
		user: User email or None (defaults to frappe.session.user).

	Returns:
		str: SQL condition fragment.

	Exceptions:
		None.

	Side effects:
		None.
	"""
	if not user:
		user = frappe.session.user
	if not user or user == "Guest":
		return "1=0"

	user_roles = set(frappe.get_roles(user))
	if (
		user == "Administrator"
		or any(r in user_roles for r in BROADCAST_READ_ALL_ROLES)
		or any(r in user_roles for r in BROADCAST_ALL_CHANNEL_ROLES)
	):
		return ""

	permitted = {ch for ch, roles in CHANNEL_ROLES.items() if any(r in user_roles for r in roles)}
	if not permitted:
		return "1=0"

	quoted = ", ".join(frappe.db.escape(c) for c in sorted(permitted))
	return f"exists (select 1 from `tabBroadcast Channel` bc where bc.parent = `tabBroadcast`.name and bc.channel in ({quoted}))"


def recipient_query_conditions(user: str | None = None) -> str:
	"""Return permission_query_conditions SQL for Broadcast Recipient.

	Returns empty string for read-all roles, all-channel roles, or Administrator.
	Returns channel IN clause for permitted channels.
	Returns '1=0' for users with no channel role and Guest.

	Params:
		user: User email or None (defaults to frappe.session.user).

	Returns:
		str: SQL condition fragment.

	Exceptions:
		None.

	Side effects:
		None.
	"""
	if not user:
		user = frappe.session.user
	if not user or user == "Guest":
		return "1=0"

	user_roles = set(frappe.get_roles(user))
	if (
		user == "Administrator"
		or any(r in user_roles for r in BROADCAST_READ_ALL_ROLES)
		or any(r in user_roles for r in BROADCAST_ALL_CHANNEL_ROLES)
	):
		return ""

	permitted = {ch for ch, roles in CHANNEL_ROLES.items() if any(r in user_roles for r in roles)}
	if not permitted:
		return "1=0"

	quoted = ", ".join(frappe.db.escape(c) for c in sorted(permitted))
	return f"`tabBroadcast Recipient`.channel in ({quoted})"
