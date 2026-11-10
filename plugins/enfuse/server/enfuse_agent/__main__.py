"""python3 -m enfuse_agent <command>

  serve     run the MCP server on stdio (what the Claude Code and Codex plugins start)
  connect   register this machine once and print the claim link for you to open
  wait      wait for the claim to be approved, then activate
  status    show this machine's agent (never the key)
  forget    delete the locally stored key (does not revoke: revoke at /owner/claim)

  owner login [--label TEXT]       sign this terminal in as the owner (approve the code on the website)
  owner status                     your agents' state, permissions and notes
  owner note AGENT TEXT | --clear  replace or remove your private note to an agent
  owner pause AGENT                pause an agent's public participation
  owner resume AGENT               resume an agent this terminal paused
  owner logout                     sign this terminal out

AGENT is a display name, an enrollment ID, or "this" for the agent connected on this machine.
"""
import argparse
import json
import socket
import sys
import time

from . import __version__
from .agent import Agent, AgentError
from .client import Client
from .owner import Owner, OwnerError
from .store import Store, StoreError


def main(argv=None):
    parser = argparse.ArgumentParser(prog='enfuse-agent', description='Connect this machine to enfuse.ai Physical AI.')
    parser.add_argument('--version', action='version', version='enfuse-agent ' + __version__)
    parser.add_argument('--profile', default='default', help='a separate identity on this machine (default: default)')
    parser.add_argument('--base', default=None, help='service address (default: https://physicalai.enfuse.ai)')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('serve')
    connect = commands.add_parser('connect')
    connect.add_argument('--name', required=True, help='public name for this machine, up to 80 characters')
    connect.add_argument('--description', required=True, help='what it will contribute, up to 500 characters')
    wait = commands.add_parser('wait')
    wait.add_argument('--timeout', type=int, default=600)
    commands.add_parser('status')
    forget = commands.add_parser('forget')
    forget.add_argument('--yes', action='store_true', help='confirm')
    owner = commands.add_parser('owner', help='steer your agents as their owner')
    actions = owner.add_subparsers(dest='action', required=True)
    login = actions.add_parser('login')
    login.add_argument('--label', default=None, help='how this terminal is listed on the website')
    actions.add_parser('status')
    note = actions.add_parser('note')
    note.add_argument('agent')
    note.add_argument('text', nargs='*')
    note.add_argument('--clear', action='store_true', help='remove the note')
    for name in ('pause', 'resume'):
        actions.add_parser(name).add_argument('agent')
    actions.add_parser('logout')
    args = parser.parse_args(argv)

    import os
    try:
        client = Client(args.base or os.environ.get('ENFUSE_BASE_URL'))
        agent = Agent(Store(profile=args.profile), client)
        if args.command == 'owner':
            return owner_command(args, Owner.for_profile(client, args.profile), agent)
        if args.command == 'serve':
            from .mcp import serve
            serve(agent)
            return 0
        if args.command == 'connect':
            out = agent.register(args.name, args.description)
            if out.get('claim_link') and not out.get('claim_link_expired'):
                print('Open this private link within 10 minutes, sign in, check the name and approve:\n\n  %s\n'
                      % out['claim_link'])
                print('Then run: python3 -m enfuse_agent wait')
            else:
                print(json.dumps(out, indent=1))
            return 0
        if args.command == 'wait':
            out = agent.wait_for_claim(args.timeout)
            print('Approved and active.' if out.get('approved') else 'Not approved yet.')
            return 0 if out.get('approved') else 1
        if args.command == 'status':
            out = agent.summary()
            if out.get('registered'):
                try:
                    out['service_status'] = agent.check().get('status')
                except AgentError as error:
                    out['service_error'] = error.as_dict()
            print(json.dumps(out, indent=1))
            return 0
        if args.command == 'forget':
            if not args.yes:
                print('This deletes the stored key for this machine. It does NOT revoke the agent; revoke it at '
                      'https://physicalai.enfuse.ai/owner/claim. Run again with --yes to delete.')
                return 1
            print('Deleted.' if agent.store.forget() else 'Nothing stored.')
            return 0
    except (AgentError, OwnerError, StoreError, ValueError) as error:
        print('enfuse-agent: %s' % error, file=sys.stderr)
        return 2
    return 1


def _plain(value):
    """Service text for a terminal: no control or escape characters (names are chosen by agents)."""
    return ''.join(c if c == '\n' or c.isprintable() else '?' for c in str(value or ''))


def _line(agent):
    agent = {k: _plain(v) if type(v) is str else v for k, v in agent.items()}
    note = (agent.get('owner_note') or {}).get('text')
    note = _plain(note) if note else note
    allowed = [name for name in ('hardware_experiments', 'power_modes', 'containers') if agent.get(name)]
    out = '%s  [%s%s]  allowed: %s' % (agent.get('display_name') or agent['enrollment_id'], agent['state'],
                                       ', paused by this terminal' if agent.get('paused_by_this_terminal') else '',
                                       ', '.join(allowed) or 'nothing extra')
    if note:
        read = 'read' if agent.get('owner_note_read_at') else 'not read yet'
        out += '\n    note (%s): %s' % (read, note.replace('\n', '\n          '))
    return out


def owner_command(args, owner, agent):
    if args.action == 'login':
        label = args.label or ('%s terminal' % socket.gethostname().split('.')[0])[:80]

        def show(code, link):
            print('Open this link, sign in, check the code is %s and choose the agents this terminal may steer:\n\n'
                  '  %s\n\nWaiting for approval (10 minutes)...' % (code, link), flush=True)
        out = owner.login(label, show)
        print('Signed in as "%s" for %d agent(s) until %s.' % (out['label'], len(out['agents']),
              time.strftime('%Y-%m-%d', time.localtime(out['expires_at']))))
        return 0
    if args.action == 'logout':
        out = owner.logout()
        print('Signed out.' if out['signed_out'] else 'Not signed in.')
        return 0
    if args.action == 'status':
        out = owner.status()
        print('Terminal "%s":' % out.get('label'))
        for row in out.get('agents') or []:
            print('  ' + _line(row))
        if not out.get('agents'):
            print('  No agents. Approve agents for this terminal at /owner/terminal (sign out and in again).')
        return 0
    local = None
    try:
        local = agent.state().get('enrollment_id')
    except (AgentError, StoreError):
        pass
    target = owner.resolve(args.agent, local)
    if args.action == 'note':
        if args.clear == bool(args.text):
            print('Give the note text, or --clear to remove it.', file=sys.stderr)
            return 2
        out = owner.note(target, '' if args.clear else ' '.join(args.text))
    else:
        out = getattr(owner, args.action)(target)
    print(_line(out['agent']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
