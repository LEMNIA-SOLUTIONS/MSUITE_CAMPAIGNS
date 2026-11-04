"""
Performance Labels — tenant-level overlay for the Metric Catalog.

Single doctype storing the marketer's preferred display strings for
the catalog's metric keys. Reads route through
`performance/labels.py:resolve_label`, which falls back to the
catalog defaults whenever a field here is blank.
"""

from frappe.model.document import Document


class PerformanceLabels(Document):
	def on_update(self):
		# Bust BOTH caches so the next overlay read picks up the new
		# value:
		#   • `frappe.local._performance_labels` — request-scoped
		#     overlay cache.
		#   • Frappe's `get_cached_doc` store — cross-request, keyed by
		#     (doctype, name). Without this clear the overlay reader
		#     would keep returning the pre-save doc.
		import frappe

		frappe.local._performance_labels = None
		frappe.clear_document_cache(self.doctype, self.name)
