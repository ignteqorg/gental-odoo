import { Component, onWillStart, useState } from "@odoo/owl";
import { localization } from "@web/core/l10n/localization";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const PERIODS = [7, 30, 90];

export class GentalMcpDashboard extends Component {
    static template = "gental_mcp.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.periods = PERIODS;
        this.dir = localization.direction || "ltr";
        this.state = useState({ period: 7, data: null });
        onWillStart(() => this.load(7));
    }

    async load(period) {
        this.state.period = period;
        this.state.data = await this.orm.call("gental.mcp.dashboard", "get_data", [period]);
    }

    get maxPerDay() {
        const counts = this.state.data.calls.per_day.map((d) => d.count);
        return Math.max(1, ...counts);
    }

    barHeight(count) {
        return Math.max(count ? 4 : 0, Math.round((count / this.maxPerDay) * 100));
    }

    get maxTool() {
        return Math.max(1, ...this.state.data.top_tools.map((t) => t.count));
    }

    toolWidth(count) {
        return Math.round((count / this.maxTool) * 100);
    }

    errorPercent() {
        return `${Math.round(this.state.data.calls.error_rate * 1000) / 10}%`;
    }

    openAttention(row) {
        const act = row.action;
        if (act.xml_id) {
            return this.action.doAction(act.xml_id);
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: act.name || row.label,
            res_model: act.res_model,
            domain: act.domain,
            views: [[false, "list"], [false, "form"]],
        });
    }
}

registry.category("actions").add("gental_mcp_dashboard", GentalMcpDashboard);
