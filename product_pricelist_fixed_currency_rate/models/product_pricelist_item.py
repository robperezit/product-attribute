# Copyright 2023 ForgeFlow S.L. (https://www.forgeflow.com)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models


class ProductPricelist(models.Model):
    _inherit = "product.pricelist"

    def _compute_price_rule(self, products_qty_partner, date=False, uom_id=False):
        """Low-level method - Mono pricelist, multi products
        Returns: dict{product_id: (price, suitable_rule) for the given pricelist}
        """
        self.ensure_one()
        if not date:
            date = self._context.get("date") or fields.Datetime.now()
        if not uom_id and self._context.get("uom"):
            uom_id = self._context["uom"]

        # If we are in a pricelist that has items with fixed_currency_rate,
        # and those items might be applied, we need to handle it.
        # Since v14 _compute_price_rule is monolithic, we have to override it
        # or find a way to inject context per item.
        # However, _compute_price_rule calls src_currency._convert(...)
        # We can't easily change the context for that _convert call inside the loop
        # without duplicating the whole method.

        # Let's check how many items have fixed_currency_rate
        # if none, just call super.
        items_with_fixed_rate = self.item_ids.filtered(
            lambda i: i.fixed_currency_rate > 0.0
        )
        if not items_with_fixed_rate:
            return super()._compute_price_rule(
                products_qty_partner, date=date, uom_id=uom_id
            )

        # If there are items with fixed rate, we need to replicate the logic of
        # _compute_price_rule but injecting the context when needed.
        # This is a bit heavy but it's the most reliable way in v14.

        # Re-using logic from product.pricelist._compute_price_rule
        if uom_id:
            # rebrowse with uom if given
            products = [
                item[0].with_context(uom=uom_id) for item in products_qty_partner
            ]
            products_qty_partner = [
                (products[index], data_struct[1], data_struct[2])
                for index, data_struct in enumerate(products_qty_partner)
            ]
        else:
            products = [item[0] for item in products_qty_partner]

        if not products:
            return {}

        categ_ids = {}
        for p in products:
            categ = p.categ_id
            while categ:
                categ_ids[categ.id] = True
                categ = categ.parent_id
        categ_ids = list(categ_ids)

        is_product_template = products[0]._name == "product.template"
        from itertools import chain

        if is_product_template:
            prod_tmpl_ids = [tmpl.id for tmpl in products]
            # all variants of all products
            prod_ids = [
                p.id
                for p in list(
                    chain.from_iterable([t.product_variant_ids for t in products])
                )
            ]
        else:
            prod_ids = [product.id for product in products]
            prod_tmpl_ids = [product.product_tmpl_id.id for product in products]

        items = self._compute_price_rule_get_items(
            products_qty_partner, date, uom_id, prod_tmpl_ids, prod_ids, categ_ids
        )

        results = {}
        for product, qty, partner in products_qty_partner:
            results[product.id] = 0.0
            suitable_rule = False

            qty_uom_id = self._context.get("uom") or product.uom_id.id
            qty_in_product_uom = qty
            if qty_uom_id != product.uom_id.id:
                try:
                    qty_in_product_uom = (
                        self.env["uom.uom"]
                        .browse([qty_uom_id])
                        ._compute_quantity(qty, product.uom_id)
                    )
                except Exception:
                    pass

            price = product.price_compute("list_price")[product.id]

            price_uom = self.env["uom.uom"].browse([qty_uom_id])
            for rule in items:
                if rule.min_quantity and qty_in_product_uom < rule.min_quantity:
                    continue
                if is_product_template:
                    if rule.product_tmpl_id and product.id != rule.product_tmpl_id.id:
                        continue
                    if rule.product_id and not (
                        product.product_variant_count == 1
                        and product.product_variant_id.id == rule.product_id.id
                    ):
                        continue
                else:
                    if (
                        rule.product_tmpl_id
                        and product.product_tmpl_id.id != rule.product_tmpl_id.id
                    ):
                        continue
                    if rule.product_id and product.id != rule.product_id.id:
                        continue

                if rule.categ_id:
                    cat = product.categ_id
                    while cat:
                        if cat.id == rule.categ_id.id:
                            break
                        cat = cat.parent_id
                    if not cat:
                        continue

                if rule.base == "pricelist" and rule.base_pricelist_id:
                    # In v14 we call _compute_price_rule recursively
                    price = rule.base_pricelist_id._compute_price_rule(
                        [(product, qty, partner)], date, uom_id
                    )[product.id][0]
                    src_currency = rule.base_pricelist_id.currency_id
                else:
                    price = product.price_compute(rule.base)[product.id]
                    if rule.base == "standard_price":
                        src_currency = product.cost_currency_id
                    else:
                        src_currency = product.currency_id

                if src_currency != self.currency_id:
                    # HERE IS THE CHANGE: Inject fixed_currency_rate if applicable
                    ctx = {}
                    if (
                        rule.is_fixed_currency_rate_applicable
                        and rule.fixed_currency_rate
                    ):
                        ctx["fixed_currency_rate"] = rule.fixed_currency_rate

                    price = src_currency.with_context(**ctx)._convert(
                        price, self.currency_id, self.env.company, date, round=False
                    )

                if price is not False:
                    price = rule._compute_price(
                        price, price_uom, product, quantity=qty, partner=partner
                    )
                    suitable_rule = rule
                break
            results[product.id] = (price, suitable_rule and suitable_rule.id or False)
        return results


class ProductPricelistItem(models.Model):
    _inherit = "product.pricelist.item"

    fixed_currency_rate = fields.Float(
        digits=(12, 12),
        help="If set (different to 0.0), the currency conversion will "
        "ignore the actual currency rate and always use the fixed "
        "currency rate.",
    )
    inverse_fixed_currency_rate = fields.Float(
        digits=(12, 12),
        compute="_compute_inverse_fixed_currency_rate",
        inverse="_inverse_inverse_fixed_currency_rate",
        help="If set (different to 0.0), the currency conversion will "
        "ignore the actual currency rate and always use the fixed "
        "currency rate.",
    )
    is_fixed_currency_rate_applicable = fields.Boolean(
        compute="_compute_is_fixed_currency_rate_applicable"
    )
    actual_currency_rate = fields.Float(
        digits=(12, 12), compute="_compute_is_fixed_currency_rate_applicable"
    )
    inverse_actual_currency_rate = fields.Float(
        digits=(12, 12), compute="_compute_is_fixed_currency_rate_applicable"
    )
    do_inverse_currency_rate = fields.Boolean(
        compute="_compute_do_inverse_currency_rate",
        store=True,
        readonly=False,
    )
    currency_rate_tooltip = fields.Char(
        compute="_compute_currency_rate_tooltip",
    )

    @api.depends("base_pricelist_id", "base_pricelist_id.currency_id", "base")
    def _compute_is_fixed_currency_rate_applicable(self):
        for rec in self:
            applicable = (
                rec.base == "pricelist"
                and rec.base_pricelist_id
                and rec.base_pricelist_id.currency_id != rec.pricelist_id.currency_id
            )
            rec.is_fixed_currency_rate_applicable = applicable
            if applicable:
                curr_from = rec.base_pricelist_id.currency_id
                curr_to = rec.pricelist_id.currency_id
                company = rec.company_id or self.env.company
                rate = self.env["res.currency"]._get_conversion_rate(
                    curr_from, curr_to, company, rec.date_end or fields.Date.today()
                )
                rec.actual_currency_rate = rate
                rec.inverse_actual_currency_rate = 1 / rate if rate else 0.0
            else:
                rec.actual_currency_rate = 1.0
                rec.inverse_actual_currency_rate = 1.0


    @api.depends("base_pricelist_id", "base_pricelist_id.currency_id")
    def _compute_do_inverse_currency_rate(self):
        for rec in self:
            if rec.is_fixed_currency_rate_applicable:
                rec.do_inverse_currency_rate = rec.actual_currency_rate < 1.0

    @api.depends("do_inverse_currency_rate")
    def _compute_currency_rate_tooltip(self):
        for rec in self:
            if rec.do_inverse_currency_rate:
                curr_from = rec.pricelist_id.currency_id
                curr_to = rec.base_pricelist_id.currency_id
            else:
                curr_from = rec.base_pricelist_id.currency_id
                curr_to = rec.pricelist_id.currency_id
            rec.currency_rate_tooltip = _("({curr_from} to {curr_to} rates)").format(
                curr_from=curr_from.name, curr_to=curr_to.name
            )

    @api.depends("fixed_currency_rate")
    def _compute_inverse_fixed_currency_rate(self):
        for rec in self:
            rec.inverse_fixed_currency_rate = (
                1 / rec.fixed_currency_rate if rec.fixed_currency_rate else 0.0
            )

    def _inverse_inverse_fixed_currency_rate(self):
        for rec in self:
            rec.fixed_currency_rate = (
                1 / rec.inverse_fixed_currency_rate
                if rec.inverse_fixed_currency_rate
                else 0.0
            )

    def toggle_do_inverse_currency_rate(self):
        for rec in self:
            rec.do_inverse_currency_rate = not rec.do_inverse_currency_rate
