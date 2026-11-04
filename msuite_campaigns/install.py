import frappe

# Modules that used to belong to msuite_workspace.
MOVED_MODULES = ('Campaign', 'Audience', 'AWS Pipeline', 'Performance')


def before_install():
	"""Hand the moved modules over to this app on sites that already have them.

	Frappe inserts a Module Def for every line of modules.txt right after this hook, and that
	insert fails if the row exists. Removing the rows lets Frappe recreate them under this app.

	This has to stay a direct delete. `frappe.delete_doc` would run Module Def's link checks and
	file cleanup. The DocTypes, their tables and their records are not touched.
	"""
	frappe.db.delete("Module Def", {"name": ("in", MOVED_MODULES)})
