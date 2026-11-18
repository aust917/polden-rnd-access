"""Заявки на доступ к закрытой базе: проверка дублей при подаче, приглашение по метке одобрения.

Запускает GitHub Action этого репозитория. Приглашение уходит автору заявки (issue.user), а не нику из текста:
заявить доступ за другого человека нельзя. Без INVITE_TOKEN доступ к закрытой базе не проверить — Action
сообщает об этом в заявке и завершается ошибкой, ничего не меняя.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get('GITHUB_API_URL', 'https://api.github.com')
ACCESS = os.environ.get('GITHUB_REPOSITORY', '')
TARGET = os.environ.get('TARGET_REPO', 'aust917/polden-rnd')
LABEL = os.environ.get('LABEL', 'одобрено')
APPROVERS = {a.strip().lower() for a in os.environ.get('APPROVERS', 'aust917').split(',') if a.strip()}
ACCEPT = ('Ваш агент примет приглашение сам: `gh api user/repository_invitations` → '
          '`gh api -X PATCH user/repository_invitations/<id>`; затем `gh repo clone {t}`.')


class Fail(RuntimeError):
    pass


def call(method, path, token, body=None):
    """(код, json). 404 — не ошибка: так GitHub отвечает «не коллаборатор»."""
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                          'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'polden-rnd-access'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, None
        raise Fail(f'{method} {path}: HTTP {e.code} {e.read()[:300]!r}') from None


def state(login, number, gh, invite_token, api=call):
    q = urllib.parse.urlencode({'state': 'open', 'creator': login, 'per_page': 100})
    _, issues = api('GET', f'/repos/{ACCESS}/issues?{q}', gh)
    dups = sorted(i['number'] for i in issues or [] if 'pull_request' not in i and i['number'] < number)
    if not invite_token:
        return {'dups': dups, 'collaborator': None, 'invited': None}
    code, _ = api('GET', f'/repos/{TARGET}/collaborators/{login}', invite_token)
    if code not in (204, 404):
        raise Fail(f'Проверка коллаборатора: неожиданный ответ {code}')
    _, invites = api('GET', f'/repos/{TARGET}/invitations?per_page=100', invite_token)
    invited = any((i.get('invitee') or {}).get('login', '').lower() == login.lower() for i in invites or [])
    return {'dups': dups, 'collaborator': code == 204, 'invited': invited}


def decide(action, login, label, sender, s):
    """→ {'comment', 'close' (None|'completed'|'not_planned'), 'invite', 'fail'}"""
    out = {'comment': None, 'close': None, 'invite': False, 'fail': False}
    have = (f'У @{login} уже есть доступ к `{TARGET}`. Заявка закрыта; ваш агент может сразу выполнить '
            f'`gh repo clone {TARGET}`.')
    sent = f'@{login}, приглашение в `{TARGET}` уже отправлено. ' + ACCEPT.format(t=TARGET)
    if action in ('opened', 'reopened'):
        if s['dups']:
            return {**out, 'comment': f'Это повтор заявки #{s["dups"][0]}: она уже ждёт одобрения. Закрываю повтор.',
                    'close': 'not_planned'}
        if s['collaborator']:
            return {**out, 'comment': have, 'close': 'completed'}
        if s['invited']:
            return {**out, 'comment': sent, 'close': 'completed'}
        return {**out, 'comment': f'Заявка @{login} принята и ждёт одобрения. Когда её одобрят, GitHub пришлёт '
                                  'приглашение на почту аккаунта, а эта заявка закроется с комментарием.'}
    if action != 'labeled' or label != LABEL:
        return out
    if sender.lower() not in APPROVERS:
        return {**out, 'comment': f'Метку «{LABEL}» учитываю только от {", ".join(sorted(APPROVERS))}; приглашение не отправлено.'}
    if s['collaborator'] is None:
        return {**out, 'fail': True, 'comment': 'Не могу проверить доступ: в репозитории заявок не настроен секрет '
                                                'POLDEN_RND_INVITE_TOKEN. Приглашение не отправлено; метку можно '
                                                'снять и поставить заново после настройки.'}
    if s['collaborator']:
        return {**out, 'comment': have, 'close': 'completed'}
    if s['invited']:
        return {**out, 'comment': sent, 'close': 'completed'}
    return {**out, 'invite': True, 'close': 'completed',
            'comment': f'Одобрено: @{login} приглашён в `{TARGET}` с правом записи. ' + ACCEPT.format(t=TARGET)}


def apply(number, login, plan, gh, invite_token, dry, api=call):
    done = []
    if plan['invite']:
        done.append(f'PUT collaborator {login} push')
        if not dry:
            code, _ = api('PUT', f'/repos/{TARGET}/collaborators/{login}', invite_token, {'permission': 'push'})
            if code not in (201, 204):
                raise Fail(f'Приглашение не отправлено: ответ {code}')
    if plan['comment']:
        done.append('comment: ' + plan['comment'])
        if not dry:
            api('POST', f'/repos/{ACCESS}/issues/{number}/comments', gh, {'body': plan['comment']})
    if plan['close']:
        done.append(f'close: {plan["close"]}')
        if not dry:
            api('PATCH', f'/repos/{ACCESS}/issues/{number}', gh, {'state': 'closed', 'state_reason': plan['close']})
    return done


def main():
    with open(os.environ['GITHUB_EVENT_PATH'], encoding='utf-8') as f:
        event = json.load(f)
    gh, invite_token = os.environ['GH_TOKEN'], os.environ.get('INVITE_TOKEN', '')
    dry = os.environ.get('DRY_RUN') == '1'
    if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch':
        inputs = event['inputs']
        _, issue = call('GET', f'/repos/{ACCESS}/issues/{inputs["issue"]}', gh)
        action, label = inputs['action'], LABEL
    else:
        issue, action, label = event['issue'], event['action'], (event.get('label') or {}).get('name', '')
    sender = event['sender']['login']
    login, number = issue['user']['login'], issue['number']
    if issue['user'].get('type') != 'User':
        print(f'#{number}: автор {login} не пользователь — пропускаю')
        return 0
    plan = decide(action, login, label, sender, state(login, number, gh, invite_token))
    for line in apply(number, login, plan, gh, invite_token, dry):
        print(('[проверка] ' if dry else '') + line)
    if plan['fail']:
        print('::error::Не настроен секрет POLDEN_RND_INVITE_TOKEN')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
