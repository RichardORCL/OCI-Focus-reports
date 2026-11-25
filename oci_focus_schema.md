# FOCUS Cost Report Schema

Reference: [OCI Cost Reports Overview](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costusagereportsoverview.htm#costreports__focus-cost-report-schema)

OCI generates FOCUS (FinOps Open Cost and Usage Specification) cost reports as CSV files in Object Storage. The following table documents all columns present in OCI FOCUS reports, including the standard FOCUS schema and OCI-specific extensions.

---

## Standard FOCUS Schema Fields

| Column ID | Display Name | Proprietary Mapping | Type | Description |
|-----------|--------------|---------------------|------|-------------|
| AvailabilityZone | Availability Zone | product/availabilityDomain | String | Logical availability domain. Format: `LYXg:region-AD-N` or empty. |
| BilledCost | Billed Cost | cost/myCost | BigDecimal | The calculated cost after all discounts have been applied. Note: Does not include taxes and may vary from the invoice amount. |
| BillingAccountId | Billing Account ID | cost/subscriptionId | String | Subscription identifier (numeric). |
| BillingAccountName | Billing Account Name | NONE | String | Not populated in OCI reports. |
| BillingCurrency | Billing Currency | cost/currencyCode | CurrencyCode | Currency code for the cost (e.g., EUR, USD). |
| BillingPeriodEnd | Billing Period End | NONE | DateTime | The end date and time of the billing period (ISO 8601). Billing period boundaries may differ from commitment invoice dates. |
| BillingPeriodStart | Billing Period Start | NONE | DateTime | The start date and time of the billing period (ISO 8601). |
| ChargeCategory | Charge Category | lineItem/isCorrection | String | Indicates charge type: Usage (cost of usage that occurred), Adjustment (credits/corrections), or other categories. |
| ChargeDescription | Charge Description | product/description | String | SKU description of the charge (e.g., "Block Volume - Storage", "Block Volume - Backup"). |
| ChargeFrequency | Charge Frequency | NONE | String | How often a charge occurs. Typically "Usage-based" for consumption charges. |
| ChargePeriodEnd | Charge Period End | lineItem/intervalUsageEnd | DateTime | End time of the usage interval (ISO 8601). |
| ChargePeriodStart | Charge Period Start | lineItem/intervalUsageStart | DateTime | Start time of the usage interval (ISO 8601). |
| ChargeSubcategory | Charge Subcategory | NONE | String | Not populated in OCI reports. |
| CommitmentDiscountCategory | Commitment Discount Category | NONE | String | Not populated in OCI reports. |
| CommitmentDiscountId | Commitment Discount ID | NONE | String | Not populated in OCI reports. |
| CommitmentDiscountName | Commitment Discount Name | NONE | String | Not populated in OCI reports. |
| CommitmentDiscountType | Commitment Discount Type | NONE | String | Not populated; OCI does not use reserved instances. |
| EffectiveCost | Effective Cost | cost/myCost | BigDecimal | The calculated cost, inclusive of all applicable discounts. |
| InvoiceIssuer | Invoice Issuer | NONE | String | Entity issuing the invoice. Defaults to "Oracle". |
| ListCost | List Cost | NONE | BigDecimal | Calculated cost based on the current list price (before discounts). May be 0 for some charge types. |
| ListUnitPrice | List Unit Price | NONE | BigDecimal | Unit price for a single pricing unit of the SKU at current list price, exclusive of discounts. |
| PricingCategory | Pricing Category | NONE | String | Not populated in OCI reports. |
| PricingQuantity | Pricing Quantity | usage/billedQuantity | BigDecimal | Billable quantity for pricing (e.g., GB Months, OCPUs). |
| PricingUnit | Pricing Unit | cost/skuUnitDescription | String | SKU unit description (e.g., "GB Months", "OCPU Hours"). |
| Provider | Provider | NONE | String | Cloud provider. Defaults to "Oracle". |
| Publisher | Publisher | NONE | String | Publisher of the service. "Oracle" for first-party; varies for marketplace. |
| Region | Region | product/region | String | OCI region associated with the cost (e.g., eu-amsterdam-1). |
| ResourceId | Resource ID | product/resourceId | String | Unique resource OCID. Indexed with tenantId, compartmentId, region, and resourceMeter for uniqueness. |
| ResourceName | Resource Name | NONE | String | Not populated; OCI does not publish this value in cost reports. |
| ResourceType | Resource Type | NONE | String | OCI resource type (e.g., bootvolume, bootvolumebackup, bootvolumereplica, instance). |
| ServiceCategory | Service Category | NONE | String | FOCUS service classification (e.g., Storage, Compute). |
| ServiceName | Service Name | product/service | String | OCI service name (e.g., BLOCK_STORAGE, COMPUTE). |
| SkuId | SKU ID | cost/productSku | String | Unique SKU identifier (e.g., B91961, B91628). |
| SkuPriceId | SKU Price ID | NONE | String | Not populated; this construct does not exist in OCI. |
| SubAccountId | Sub Account ID | lineItem/TenantId | String | Tenancy OCID. |
| SubAccountName | Sub Account Name | NONE | String | Tenancy name (display name). |
| Tags | Tags | tags/ | JSON | Tags associated with the resource as a JSON object (key-value pairs). |
| UsageQuantity | Usage Quantity | usage/billedQuantity | BigDecimal | Billable usage quantity (same as PricingQuantity for usage-based charges). |
| UsageUnit | Usage Unit | cost/skuUnitDescription | String | SKU units description (e.g., "GB Months", "OCPU Hours"). |

---

## OCI Extension Fields

OCI adds the following columns to FOCUS reports. These are used for cost attribution, overage tracking, and compartment hierarchy.

| Column ID | Type | Description |
|-----------|------|-------------|
| oci_ReferenceNumber | String | Unique reference identifier for the cost line item. Used for traceability and linking related records. |
| oci_CompartmentId | String | OCID of the compartment where the resource resides. |
| oci_CompartmentName | String | Display name of the compartment. |
| oci_CompartmentPath | String | Full path of the compartment in the hierarchy. Added by `getFocusReports.py`; matched via `oci_CompartmentId` to compartments from IAM; empty if not found. |
| oci_OverageFlag | String | Indicates if the line represents overage charges. "Y" when overage applies; empty otherwise. |
| oci_UnitPriceOverage | BigDecimal | Unit price for overage usage when oci_OverageFlag is "Y". |
| oci_BilledQuantityOverage | BigDecimal | Billed quantity for overage usage. |
| oci_CostOverage | BigDecimal | Cost attributed to overage. |
| oci_AttributedUsage | BigDecimal | Usage quantity attributed to the resource (for cost allocation). |
| oci_AttributedCost | BigDecimal | Cost attributed to the resource (for cost allocation). |
| oci_BackReferenceNumber | String | Reference to a related or parent line item. May be empty. |

---

## Notes

- **Consolidation**: The `consolidateReports.py` script groups rows by `(ResourceId, SkuId)` and sums: `UsageQuantity`, `PricingQuantity`, `BilledCost`, `EffectiveCost`, `ListCost`, `oci_UnitPriceOverage`, `oci_BilledQuantityOverage`, `oci_CostOverage`, `oci_AttributedUsage`, `oci_AttributedCost`.
- **Charge period**: `ChargePeriodEnd` is used to determine the latest record when consolidating varying fields.
- **Tags format**: Tags are stored as a JSON object string with keys such as `OracleInternalReserved.CostCenter`, `Owner.CreatedBy`, etc.
