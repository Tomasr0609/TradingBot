#!/usr/bin/env python
"""Dashboard mínimo §9: métricas sobre operaciones Testnet acumuladas."""

import asyncio
import json
from decimal import Decimal

from sqlalchemy import select, func

from trading_bot.storage.database import get_database
from trading_bot.execution.models import ExecutedOrder


async def main():
    db = get_database()
    async with db.session() as session:
        result = await session.execute(select(ExecutedOrder).order_by(ExecutedOrder.created_at))
        orders = result.scalars().all()
        if not orders:
            print("No hay operaciones registradas aún. Ejecuta el bot en Testnet primero.")
            return

        filled = [o for o in orders if o.status == "filled"]
        rejected = [o for o in orders if o.status == "rejected"]
        errors = [o for o in orders if o.status == "error"]

        print("="*70)
        print(f" DASHBOARD TESTNET - {len(orders)} registros")
        print("="*70)
        print(f" Filled: {len(filled)} | Rejected (riesgo): {len(rejected)} | Errors: {len(errors)}")
        if filled:
            # PnL aproximado (sin comisiones reales, solo para demo)
            total_notional = sum((o.executed_price or o.signal_price) * (o.executed_size or 0) for o in filled)
            print(f" Notional total filled: ${total_notional:,.2f}")
            # Win rate simulado no disponible sin cierres; mostramos distribución
            from collections import Counter
            strat = Counter(o.strategy_name for o in filled)
            print(f" Por estrategia: {dict(strat)}")
            sym = Counter(o.symbol for o in filled)
            print(f" Por símbolo: {dict(sym)}")
        # Métricas riesgo
        from trading_bot.risk_management.models import RiskLog
        result = await session.execute(select(RiskLog))
        logs = result.scalars().all()
        if logs:
            from collections import Counter
            rules = Counter(l.triggered_rule.value for l in logs)
            print(f"\n Riesgo - decisiones: {len(logs)}")
            print(f"  Reglas disparadas: {dict(rules)}")
            dec = Counter(l.decision.value for l in logs)
            print(f"  Decisiones: {dict(dec)}")
        # List recent
        print("\n Últimas 10 órdenes:")
        for o in orders[-10:]:
            print(f"  {o.created_at} {o.symbol} {o.signal_type} {o.status} {o.risk_decision} {o.risk_rule} size={o.approved_size} price={o.signal_price} -> {o.executed_price} id={o.order_id} {o.error_message or ''}")

        print("="*70)

if __name__ == "__main__":
    asyncio.run(main())
