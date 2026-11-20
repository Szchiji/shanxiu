"""Real command/job paths use speaker candidates, never the retired live roster."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from tests.unit.test_lottery_workflow import env
from app.models import LotteryDelivery

@pytest.mark.asyncio
@pytest.mark.parametrize('entry',['scheduled','manual'])
async def test_real_route_draw_uses_own_bot_speaker_pool_and_outbox(env,entry):
    e=env;lid=e.add(counts=((101,5),))
    context=NS(bot=e.bot,args=[str(lid)],application=NS(bot_data={}))
    update=NS(effective_chat=NS(id=-100173,type='supergroup'),effective_user=NS(id=42),message=NS(reply_text=AsyncMock()))
    if entry=='scheduled':await e.routes.run_lottery_draws(context)
    else:await e.routes.cmd_lottery_draw(update,context)
    assert e.state(lid).status=='ended' and e.state(lid).winners==[101]
    e.bot.get_chat_member.assert_awaited_once_with(chat_id=-100173,user_id=101)
    e.bot.send_message.assert_awaited_once()
    assert e.call(lambda:LotteryDelivery.query.one().state)=='sent'

@pytest.mark.asyncio
@pytest.mark.parametrize('entry',['scheduled','manual'])
async def test_unknown_membership_never_marks_ended(env,entry):
    e=env;lid=e.add();e.bot.get_chat_member.side_effect=TimeoutError('offline')
    context=NS(bot=e.bot,args=[str(lid)],application=NS(bot_data={}))
    update=NS(effective_chat=NS(id=-100173,type='supergroup'),effective_user=NS(id=42),message=NS(reply_text=AsyncMock()))
    if entry=='scheduled':await e.routes.run_lottery_draws(context)
    else:await e.routes.cmd_lottery_draw(update,context)
    assert e.state(lid).status=='drawing' and e.state(lid).winners==[]
    e.bot.send_message.assert_not_awaited()
