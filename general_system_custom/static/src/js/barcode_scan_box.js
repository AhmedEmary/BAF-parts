/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { BarcodeDialog } from "@web/core/barcode/barcode_dialog";

export class BarcodeScanBox extends Component {
    setup() {
        this.notification = useService("notification");
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
    }

    // --- 1. Camera Logic ---
    onScan() {
        this.dialog.add(BarcodeDialog, {
            facingMode: "environment",
            onResult: (barcode) => this.processBarcode(barcode),
            onError: (error) => console.warn("Barcode scan error:", error),
        });
    }

    // --- 2. Manual Input Logic ---
    async onManualInput(ev) {
        const barcode = ev.target.value;
        if (barcode) {
            await this.processBarcode(barcode);
        }
    }

    onKeydown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            ev.target.blur(); // This triggers onManualInput
        }
    }

    // --- 3. Shared Processing Logic ---
    async processBarcode(barcode) {
        if (!barcode) return;

        try {
            // A. Visual Update: Update the UI field so the user sees what they scanned
            await this.props.record.update({ [this.props.name]: barcode });
            
            const result = await this.orm.call(
                this.props.record.resModel,
                'action_search_sku',
                [this.props.record.resId],
                { barcode: barcode } 
            );
            
            if (result) {
                this.action.doAction(result);
            } else {
                this.notification.add("Scanned: " + barcode, { type: "success" });
            }
        } catch (error) {
            console.error("Error processing barcode:", error);
            const errorMsg = error.message?.data?.message || error.message || "Unknown error";
            this.notification.add("Error: " + errorMsg, { type: "danger" });
        }
    }
}

BarcodeScanBox.template = "general_system_custom.BarcodeScanBox";
BarcodeScanBox.props = { ...standardFieldProps };

registry.category("fields").add("intelliwise_scan_box", {
    component: BarcodeScanBox,
});
