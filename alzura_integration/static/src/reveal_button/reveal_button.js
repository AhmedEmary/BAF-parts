/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * A button bound to a boolean field. Clicking it flips the value client-side
 * (no server round-trip), so `invisible` modifiers that depend on the field
 * re-evaluate instantly. Used to reveal/collapse the Alzura credential inputs.
 */
export class AlzuraRevealButton extends Component {
    static template = "alzura_integration.AlzuraRevealButton";
    static props = { ...standardFieldProps };

    get isRevealed() {
        return Boolean(this.props.record.data[this.props.name]);
    }

    onClick() {
        this.props.record.update({ [this.props.name]: !this.isRevealed });
    }
}

export const alzuraRevealButton = {
    component: AlzuraRevealButton,
    supportedTypes: ["boolean"],
};

registry.category("fields").add("alzura_reveal_button", alzuraRevealButton);
