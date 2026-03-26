import asyncio
import sqlalchemy as sa
from app.dependencies import _engine

async def check():
    async with _engine.connect() as conn:
        rows = (await conn.execute(sa.text(
            'SELECT p.id, p.name, p.team, p.position_group, m.current_rating '
            'FROM players p '
            'JOIN lmsr_market_state m ON p.id = m.player_id '
            'ORDER BY m.current_rating DESC'
        ))).all()
        print('Total players: ' + str(len(rows)))
        for r in rows:
            print(str(r.id) + ' | ' + r.name + ' | ' + r.team + ' | ' + str(r.position_group) + ' | ' + str(round(float(r.current_rating), 2)))
    await _engine.dispose()

asyncio.run(check())
