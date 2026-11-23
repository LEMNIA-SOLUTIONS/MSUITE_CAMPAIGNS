# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast channel adapter registry.

Resolves channel adapters registered via `broadcast_channel_adapters` in `hooks.py`.
Adapters provide message sending, manifest generation, channel validation, and
outbound recording for unified Broadcasts.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from msuite_core.constants import LOGGER_NAME

logger = frappe.logger(LOGGER_NAME)

_HOOK = "broadcast_channel_adapters"
_CACHE_KEY = "broadcast_channel_adapters"


def get_channel_adapters() -> dict[str, dict[str, Any]]:
	"""Return `{channel_name: ADAPTER}` for every registered broadcast channel.

	Resolved from `hooks.py`'s `broadcast_channel_adapters` list (dotted paths
	to each channel's `ADAPTER` dict) and cached for the request in `frappe.local`.
	Invalid dotted paths or malformed adapter dictionaries are logged and skipped.

	Returns:
		dict[str, dict[str, Any]]: Mapping of channel names (e.g. 'WhatsApp', 'Email', 'SMS')
			to adapter dicts.

	Raises:
		None.

	Side effects:
		Caches resolved adapters in `frappe.local`.
	"""
	cached = getattr(frappe.local, _CACHE_KEY, None)
	if cached is not None:
		return cached

	adapters: dict[str, dict[str, Any]] = {}
	for dotted_path in frappe.get_hooks(_HOOK) or []:
		try:
			adapter = frappe.get_attr(dotted_path)
		except Exception:
			logger.error(f"broadcast_channel_adapters: failed to load {dotted_path}", exc_info=True)
			continue

		if not isinstance(adapter, dict):
			logger.error(f"broadcast_channel_adapters: {dotted_path} is not a dict")
			continue

		channel = adapter.get("channel")
		if not channel:
			logger.error(f"broadcast_channel_adapters: {dotted_path} has no 'channel' key")
			continue

		adapters[channel] = adapter

	setattr(frappe.local, _CACHE_KEY, adapters)
	return adapters


def get_channel_adapter(channel: str) -> dict[str, Any] | None:
	"""Retrieve registered adapter for the specified channel, or None if not found.

	Args:
		channel: Channel name ('WhatsApp', 'Email', 'SMS', etc.).

	Returns:
		dict[str, Any] | None: The adapter dictionary, or None if not registered.

	Raises:
		None.

	Side effects:
		None.
	"""
	return get_channel_adapters().get(channel)


def require_channel_adapter(channel: str) -> dict[str, Any]:
	"""Retrieve registered adapter for the specified channel or raise ValidationError.

	Args:
		channel: Channel name ('WhatsApp', 'Email', 'SMS', etc.).

	Returns:
		dict[str, Any]: The adapter dictionary.

	Raises:
		frappe.ValidationError: If channel is not installed or registered on this site.

	Side effects:
		None.
	"""
	adapter = get_channel_adapter(channel)
	if not adapter:
		frappe.throw(
			_("{0} is not installed on this site").format(channel),
			frappe.ValidationError,
		)
	return adapter
