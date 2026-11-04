frappe.views.calendar["Campaign Calendar Event"] = {
	field_map: {
		start: "start_date",
		end: "end_date",
		id: "name",
		title: "title",
		allDay: "all_day",
		color: "event_type"
	},
	get_events_method: "frappe.desk.calendar.get_events",
	get_css_class: function(d) {
		if (d.color === "Campaign") return "orange";
		if (d.color === "Social Post") return "blue";
		if (d.color === "Ad Campaign") return "purple";
		if (d.color === "WhatsApp Message") return "green";
		return "gray";
	}
};
