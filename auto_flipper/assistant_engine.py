"""Reviewed candidate workflow. Unknown external outcomes keep durable reservations."""
import asyncio
import time
from auto_flipper.database import db
from auto_flipper.economics import kopecks
from auto_flipper.safety import validate_review, action_allowed, reject_account_observation_purchase
from resale_intelligence.models.risk_gate import RiskSnapshot, assess_purchase


class AssistantWorkflow:
    def can_act(self, action):
        stopped = bool(self.is_emergency_stopped)
        if not stopped:
            try:
                database = getattr(self, 'db', db)
                if hasattr(database, 'is_emergency_stopped'):
                    stopped = bool(database.is_emergency_stopped())
            except Exception:
                pass
        return action_allowed(self.mode, stopped, action)

    def evaluate_reviewed_candidate(self, candidate):
        payload, review = candidate['payload'], candidate['review']
        reject_account_observation_purchase(candidate)
        if payload.get('canonical_sku', '').startswith('tf2_disc:') or payload.get('category_id', '').startswith('tf2_disc:'):
            raise PermissionError('DISCOVERY_CANDIDATE_NOT_PURCHASABLE: Discovery candidates cannot be purchased')
        if not review:
            raise ValueError('NEEDS_EVIDENCE: сначала /review')
        validate_review(review)
        if not 0 <= time.time()-candidate['observed_at'] <= 60:
            raise ValueError('STALE_CANDIDATE: нужен новый срез')
        if not 0 <= time.time()-candidate['reviewed_at'] <= 60:
            raise ValueError('STALE_REVIEW: обновите расчёт и финансовый снимок')
        evaluation = self.evaluate_candidate_deal(**payload, review=review)
        if not evaluation.is_eligible:
            raise ValueError('; '.join(evaluation.rejection_reasons))
        return evaluation

    async def approve_candidate(self, lot_id, actor):
        if not db.is_user_admin(actor):
            raise PermissionError('Admin required')
        if self.mode != 'ASSIST' or not self.can_act('checkout'):
            raise ValueError('ASSIST required')
        candidate = db.get_candidate(lot_id)
        if not candidate:
            raise ValueError('Unknown candidate')
        return await self._execute_reviewed(candidate)

    def prepare_manual_purchase(self, lot_id, actor):
        """Reserve one reviewed purchase without submitting a payment."""
        if not db.is_user_admin(actor) or self.mode != 'ASSIST' or not self.can_act('checkout'):
            raise ValueError('Admin and ASSIST required')
        candidate = db.get_candidate(lot_id)
        if not candidate:
            raise ValueError('Unknown candidate')
        evaluation = self.evaluate_reviewed_candidate(candidate)
        intent = db.claim_purchase(lot_id, evaluation.category_id, candidate['payload']['price'],
            self.dry_run, candidate['reviewed_at'], candidate=candidate, emergency_stopped=self.is_emergency_stopped)
        if not intent:
            raise ValueError('PURCHASE_ALREADY_CLAIMED_OR_RECONCILIATION_REQUIRED')
        return intent

    async def publish_checked_item(self, item_id, actor, title, description):
        if not db.is_user_admin(actor) or not self.can_act('publish'):
            raise PermissionError('Publishing requires admin and ASSIST/LIMITED_AUTO')
        item = db.get_inventory_item(item_id)
        if not item or item['status'] != 'ready_for_sale' or bool(item['is_dry_run']) != self.dry_run:
            raise ValueError('Item is not ready in this environment')
        if item['sell_price'] <= 0:
            raise ValueError('VERIFIED_LISTING_PRICE_REQUIRED: use exact buyer route or review a listing price')
        with db._lock, db._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            checked = conn.execute('SELECT 1 FROM inventory_checks WHERE item_uuid=?',(item_id,)).fetchone()
            if not checked:
                raise ValueError('Intake evidence required')
            changed = conn.execute("UPDATE flipper_inventory SET status='publishing' WHERE item_uuid=? AND status='ready_for_sale'",(item_id,))
            if changed.rowcount != 1:
                raise ValueError('Publication already claimed')
        try:
            result = await self.client.save_offer(node_id=item['node_id'],title=title,
                desc=description,price=item['sell_price'],dry_run=self.dry_run)
        except (Exception, asyncio.CancelledError):
            db.update_inventory(item_id,status='publication_unknown')
            raise
        offer_id = result.get('offer_id')
        if result.get('success') and offer_id and offer_id not in ('new','resale_lot'):
            # If paused during the request, retain identity for a subsequent stop sweep.
            state = 'listed' if self.can_act('publish') else 'emergency_paused'
            db.update_inventory(item_id,status=state,resale_lot_id=str(offer_id))
            if state == 'emergency_paused':
                await self.stop_and_deactivate()
        else:
            db.update_inventory(item_id,status='publication_unknown')
        return result

    async def _execute_reviewed(self, candidate):
        evaluation = self.evaluate_reviewed_candidate(candidate)
        payload, review = candidate['payload'], candidate['review']
        dry_run = self.dry_run  # Pin environment for the whole operation.
        if not self.can_act('checkout'):
            return None
        from auto_flipper.trade_admission import check_capital
        if self.mode == 'LIMITED_AUTO' and (review['product']['transfer_method'] == 'manual'
                or review['product']['kind'] in ('trade_item','permanent_key')):
            raise ValueError('MANUAL_ROUTE_REQUIRES_ASSIST')
        actual_available = None
        if not dry_run:
            if self.client.checkout_quote_validator is None:
                raise ValueError('CHECKOUT_QUOTE_UNVERIFIED: use /prepare for a manual purchase')
            account = await self.client.get_account_info()
            if account.get('is_authenticated') is not True or 'balance_available' not in account:
                raise ValueError('BALANCE_UNKNOWN')
        check_capital(db, dry_run, review, evaluation.category_id, payload['price'],
                      available_cash=actual_available, emergency_stopped=self.is_emergency_stopped)
        # Fresh check after network reads, immediately before the durable reservation.
        self.evaluate_reviewed_candidate(candidate)
        if not self.can_act('checkout') or dry_run != self.dry_run:
            return None
        reserved_capital = db.capital_status(dry_run)
        intent_id = db.claim_purchase(payload['lot_id'],evaluation.category_id,payload['price'],dry_run,candidate['reviewed_at'],candidate=candidate, emergency_stopped=self.is_emergency_stopped)
        if not intent_id:
            raise ValueError('PURCHASE_ALREADY_CLAIMED_OR_RECONCILIATION_REQUIRED')
        try:
            def preflight():
                # Called after the client's network prefetch, immediately before POST.
                fresh = db.get_candidate(payload['lot_id'])
                if not fresh or fresh['reviewed_at'] != candidate['reviewed_at'] or fresh['payload'] != payload:
                    raise ValueError('REVIEW_CHANGED_BEFORE_PAYMENT')
                self.evaluate_reviewed_candidate(fresh)
                if not self.can_act('checkout') or dry_run != self.dry_run:
                    raise ValueError('MODE_CHANGED_BEFORE_PAYMENT')
                if self.is_emergency_stopped or (hasattr(db, 'is_emergency_stopped') and db.is_emergency_stopped()):
                    raise ValueError('EMERGENCY_STOP_BEFORE_PAYMENT')
                capital = db.capital_status(dry_run)
                if (capital['ledger_fingerprint'] != reserved_capital['ledger_fingerprint']
                        or not capital['balance_verified'] or not capital['purchasing_available']
                        or capital['available_cash'] < reserved_capital['available_cash']):
                    raise ValueError('CAPITAL_CHANGED_BEFORE_PAYMENT')
            result = await self.client.checkout_lot(payload['lot_id'],payload['price'],dry_run=dry_run,preflight=preflight,expected_sku=review['sku'])
        except (Exception, asyncio.CancelledError):
            db.update_purchase_intent(intent_id,status='unknown',error_message='Checkout interrupted; reconcile before retry')
            raise
        if result.get('status') == 'UNKNOWN' or result.get('unknown_state'):
            db.update_purchase_intent(intent_id,status='unknown',error_message='Payment outcome unknown')
            return None
        if not result.get('success'):
            db.update_purchase_intent(intent_id,status='failed',error_message=str(result.get('error','Checkout rejected')))
            return None
        if not result.get('order_id'):
            db.update_purchase_intent(intent_id,status='unknown',error_message='Missing order identity')
            return None
        # Store server order identity before finalization; unresolved executed rows
        # remain blocking if finalization crashes. No generated fallback order IDs.
        db.update_purchase_intent(intent_id,status='executed',order_id=result['order_id'])
        if not dry_run:
            # The legacy adapter returns the requested price, not a verified debit.
            # Preserve the real order identity and wait for statement reconciliation.
            return None
        item_id = db.complete_purchase(intent_id,result['order_id'],payload,evaluation,dry_run)
        if self.can_act('publish') and self.dry_run == dry_run:
            await self._process_post_purchase(item_id,result['order_id'],payload['title'],
                                              evaluation.sell_price,evaluation.category_id)
        return item_id
