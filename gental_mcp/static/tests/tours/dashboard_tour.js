import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("gental_mcp_dashboard_tour", {
    steps: () => [
        { trigger: ".o_gmcp_dashboard .o_gmcp_kpi:nth-child(4)" },
        { trigger: ".o_gmcp_period:contains('30')", run: "click" },
        { trigger: ".o_gmcp_period.active:contains('30')" },
    ],
});

registry.category("web_tour.tours").add("gental_mcp_dashboard_rtl_tour", {
    steps: () => [{ trigger: ".o_gmcp_dashboard[dir=rtl] .o_gmcp_kpi:nth-child(4)" }],
});
