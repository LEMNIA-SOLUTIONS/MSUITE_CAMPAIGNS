"""Unit tests for broadcast constants and shared types.

Tests validation rules and invariants for channels, statuses, transitions,
and recipient ranking.
"""

import unittest

from msuite_campaigns.broadcast.constants import (
	ALLOWED_TRANSITIONS,
	BROADCAST_STATUSES,
	CHANNEL_STATUSES,
	CHANNELS,
	RECIPIENT_INSERT_BATCH,
	RECIPIENT_STATUS_RANK,
	RECIPIENT_STATUSES,
)


class TestBroadcastConstants(unittest.TestCase):
	"""Test suite validating broadcast constants and state machine definitions.

	Side effects: None.
	"""

	def test_channels_exact_members(self):
		"""Validate that CHANNELS contains exactly WhatsApp, Email, and SMS.

		Params: None.
		Returns: None.
		Raises: AssertionError if channels do not match expected tuple.
		Side effects: None.
		"""
		expected = ("WhatsApp", "Email", "SMS")
		self.assertEqual(CHANNELS, expected)
		self.assertEqual(len(CHANNELS), 3)

	def test_broadcast_and_channel_statuses(self):
		"""Validate that BROADCAST_STATUSES and CHANNEL_STATUSES are identical and complete.

		Params: None.
		Returns: None.
		Raises: AssertionError if statuses do not match specification.
		Side effects: None.
		"""
		expected = (
			"Draft",
			"Scheduled",
			"Queued",
			"In Progress",
			"Paused",
			"Completed",
			"Partially Failed",
			"Failed",
			"Cancelled",
		)
		self.assertEqual(BROADCAST_STATUSES, expected)
		self.assertEqual(CHANNEL_STATUSES, expected)

	def test_every_transition_target_is_a_known_status(self):
		"""Validate that every key and transition target in ALLOWED_TRANSITIONS is a known status.

		Params: None.
		Returns: None.
		Raises: AssertionError if any transition key or target status is unknown.
		Side effects: None.
		"""
		known_statuses = set(BROADCAST_STATUSES)
		self.assertEqual(set(ALLOWED_TRANSITIONS.keys()), known_statuses)

		for source_status, target_statuses in ALLOWED_TRANSITIONS.items():
			self.assertIsInstance(
				target_statuses,
				set,
				f"Transitions for {source_status} must be a set",
			)
			for target in target_statuses:
				self.assertIn(
					target,
					known_statuses,
					f"Unknown transition target '{target}' from '{source_status}'",
				)

	def test_terminal_states_have_no_outgoing_transitions(self):
		"""Validate that terminal states (Completed, Cancelled) have empty transition sets.

		Params: None.
		Returns: None.
		Raises: AssertionError if terminal states have outgoing transitions or non-terminals are empty.
		Side effects: None.
		"""
		terminal_states = {"Completed", "Cancelled"}
		for status, targets in ALLOWED_TRANSITIONS.items():
			if status in terminal_states:
				self.assertEqual(
					targets,
					set(),
					f"Terminal status '{status}' must have no outgoing transitions",
				)
			else:
				self.assertTrue(
					len(targets) > 0,
					f"Non-terminal status '{status}' should have at least one outgoing transition",
				)

	def test_allowed_transitions_exact_mapping(self):
		"""Validate exact allowed state transitions for broadcast state machine.

		Params: None.
		Returns: None.
		Raises: AssertionError if transitions differ from specification.
		Side effects: None.
		"""
		expected_transitions = {
			"Draft": {"Scheduled", "Queued", "Cancelled"},
			"Scheduled": {"Draft", "Queued", "Cancelled"},
			"Queued": {"In Progress", "Paused", "Cancelled", "Failed"},
			"In Progress": {"Paused", "Completed", "Partially Failed", "Failed", "Cancelled"},
			"Paused": {"Queued", "In Progress", "Cancelled"},
			"Partially Failed": {"Queued"},
			"Failed": {"Queued"},
			"Completed": set(),
			"Cancelled": set(),
		}
		self.assertEqual(ALLOWED_TRANSITIONS, expected_transitions)

	def test_recipient_statuses_defined(self):
		"""Validate that RECIPIENT_STATUSES contains all specified recipient statuses.

		Params: None.
		Returns: None.
		Raises: AssertionError if statuses do not match specification.
		Side effects: None.
		"""
		expected = (
			"Queued",
			"Sent",
			"Delivered",
			"Read",
			"Failed",
			"Bounced",
			"Retrying",
			"Skipped",
			"Unsubscribed",
		)
		self.assertEqual(RECIPIENT_STATUSES, expected)

	def test_recipient_status_rank_mapping_and_order(self):
		"""Validate RECIPIENT_STATUS_RANK bijection and monotonic ordering.

		Verifies:
		- Every RECIPIENT_STATUSES member has a rank and vice versa.
		- Rank order Queued < Sent < Delivered < Read.
		- Retrying is ranked between Queued and Sent.
		- Failed, Bounced, Skipped, Unsubscribed are ranked terminal (5).

		Params: None.
		Returns: None.
		Raises: AssertionError if ranking invariants are violated.
		Side effects: None.
		"""
		self.assertEqual(set(RECIPIENT_STATUS_RANK.keys()), set(RECIPIENT_STATUSES))

		# Strict ascending order for happy-path delivery progression
		self.assertLess(RECIPIENT_STATUS_RANK["Queued"], RECIPIENT_STATUS_RANK["Retrying"])
		self.assertLess(RECIPIENT_STATUS_RANK["Retrying"], RECIPIENT_STATUS_RANK["Sent"])
		self.assertLess(RECIPIENT_STATUS_RANK["Sent"], RECIPIENT_STATUS_RANK["Delivered"])
		self.assertLess(RECIPIENT_STATUS_RANK["Delivered"], RECIPIENT_STATUS_RANK["Read"])

		# Verify exact ranks
		expected_ranks = {
			"Queued": 0,
			"Retrying": 1,
			"Sent": 2,
			"Delivered": 3,
			"Read": 4,
			"Failed": 5,
			"Bounced": 5,
			"Skipped": 5,
			"Unsubscribed": 5,
		}
		self.assertEqual(RECIPIENT_STATUS_RANK, expected_ranks)

	def test_recipient_insert_batch_size(self):
		"""Validate RECIPIENT_INSERT_BATCH is set to 1000.

		Params: None.
		Returns: None.
		Raises: AssertionError if batch size is not 1000.
		Side effects: None.
		"""
		self.assertEqual(RECIPIENT_INSERT_BATCH, 1000)

	def test_channel_roles_and_admin_roles_defined(self):
		"""Validate CHANNEL_ROLES, BROADCAST_ADMIN_ROLES, and BROADCAST_READ_ALL_ROLES.

		Params: None.
		Returns: None.
		Raises: AssertionError if constants do not match specification.
		Side effects: None.
		"""
		from msuite_campaigns.broadcast.constants import (
			BROADCAST_ADMIN_ROLES,
			BROADCAST_READ_ALL_ROLES,
			CHANNEL_ROLES,
		)

		expected_channel_roles = {
			"WhatsApp": ("WhatsApp Manager", "WhatsApp User"),
			"Email": ("Email Manager", "Email User"),
			"SMS": ("SMS Manager", "SMS User"),
		}
		self.assertEqual(CHANNEL_ROLES, expected_channel_roles)

		expected_admin_roles = ("System Manager", "MSuite Admin", "Campaign Manager")
		self.assertEqual(BROADCAST_ADMIN_ROLES, expected_admin_roles)

		expected_read_all = expected_admin_roles + ("MSuite Viewer",)
		self.assertEqual(BROADCAST_READ_ALL_ROLES, expected_read_all)

