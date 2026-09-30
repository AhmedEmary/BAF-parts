/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * A button bound to a boolean field. Clicking it flips the value client-side
 * (no server round-trip), so `invisible` modifiers that depend on the field
 * re-evaluate instantly. Used to reveal/collapse integration credential inputs.
 */
export class BafRevealButton extends Component {
    static template = "general_system_custom.BafRevealButton";
    static props = { ...standardFieldProps };

    get isRevealed() {
        return Boolean(this.props.record.data[this.props.name]);
    }

    onClick() {
        this.props.record.update({ [this.props.name]: !this.isRevealed });
    }
}

export const bafRevealButton = {
    component: BafRevealButton,
    supportedTypes: ["boolean"],
};

registry.category("fields").add("baf_reveal_button", bafRevealButton);
