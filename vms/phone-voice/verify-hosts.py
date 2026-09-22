"""Manual AudioSocket verification on VM 233; creates test sessions, never rings the handset."""

import asyncio
from control import request
from live_bridge import Connection, until


async def state(number):
    return await request('session', session_id=number)


async def finished(number):
    result = await state(number)
    if result['state'] == 'error':
        raise AssertionError(result['error'])
    return result if result['state'] == 'done' else None


async def command_started(number):
    result = await state(number)
    if result['state'] == 'error':
        raise AssertionError(result['error'])
    return result['state'] == 'running' and 'command' in result['progress'].lower()


async def verify(host, digit, expected):
    call = await Connection().open(0)
    number = call.status()['session_id']
    try:
        assert (await state(number))['host'] == 'choose'
        await call.key(digit)
        async def chosen():
            return (await state(number))['host'] == host
        await until(chosen, timeout=5)
        await call.key('*')
        await request('submit', session_id=number, text=(
            'This is an automated host routing and hangup test. Run the shell command '
            '`hostname; pwd; sleep 12`. After it finishes, reply with the actual hostname '
            'and directory followed by "hangup passed". Do not change files, use other tools, or ask questions.'))
        await until(lambda: command_started(number), timeout=120)
        thread = (await state(number))['thread_id']
        await call.close()
        call = None
        await asyncio.sleep(1)
        assert (await state(number))['state'] == 'running'
        result = await until(lambda: finished(number), timeout=180)
        assert expected.lower() in result['last_reply'].lower(), result['last_reply']
        assert 'hangup passed' in result['last_reply'].lower(), result['last_reply']
        assert result['thread_id'] == thread
        print(f"PASS {result['extension']}: {host} shell execution and hangup: {result['last_reply']}", flush=True)
        call = await Connection().open(number)
        await until(lambda: len(call.audio) >= 6400)
        assert (await state(number))['host'] == host
        assert (await state(number))['thread_id'] == thread
        await call.key('*')
        print(f'PASS {host}: callback retained host and conversation and returned audio', flush=True)
        if host == 'workstation':
            await request('submit', session_id=number, text=(
                'Automated native phone question test. Call phone_ask_user with question '
                '"For this automatic host test, choose blue or green." and options ["Blue", "Green"]. '
                'After the owner answers, briefly say which color they chose. Use no other tools.'))
            await until(lambda: 'automatic host test' in call.status().get('spoken_text', '').lower(), timeout=120)
            await call.key('2')
            result = await until(lambda: finished(number), timeout=120)
            assert 'green' in result['last_reply'].lower(), result
            print('PASS workstation: Codex clarification reached the phone and received Green', flush=True)
            await call.key('*')
            await request('submit', session_id=number, text=(
                'Automated interruption test. Run shell command sleep 60. Use no other tools and ask no questions.'))
            await until(lambda: command_started(number), timeout=120)
            await call.key('*')
            await asyncio.sleep(.15)
            await call.key('*')
            async def interrupted():
                return (await state(number))['state'] == 'interrupted'
            await until(interrupted, timeout=15)
            assert (await state(number))['thread_id'] == thread
            print('PASS workstation: double star stopped its actual task and kept the same thread', flush=True)
        return number
    finally:
        if (await state(number))['state'] in ('running', 'waiting'):
            await request('interrupt', session_id=number)
        if call:
            await call.close()


async def main():
    await verify('proxmox', '1', '/var/lib/codex-phone/workspace')
    await verify('workstation', '2', '/home/raygen/Projects/cisco-phone-shenanigans')

asyncio.run(main())
