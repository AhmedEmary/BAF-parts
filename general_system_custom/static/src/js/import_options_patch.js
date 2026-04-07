/** @odoo-module **/

import { ImportDataOptions } from "@base_import/import_data_options/import_data_options";
import { patch } from "@web/core/utils/patch";
import { onMounted } from "@odoo/owl";

patch(ImportDataOptions.prototype, {
    setup() {
        super.setup();
        
        onMounted(() => {
            const fieldName = this.props.fieldInfo.name;
            const targetFields = ['brand_id', 'product_id']; // List of fields to change default for

            if (targetFields.includes(fieldName)) {
                const createOption = this.state.options.find(o => o[0] === 'create');
                if (createOption) {
                    this.onSelectionChanged({ target: { value: 'create' } });
                }
            }
        });
    }
});
