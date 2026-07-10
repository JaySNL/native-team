# native-team

A file-based bus for running a `claude` lead and interactive `qwen` grunts in adjacent tmux panes.

    team init          # create .team/, install grunt qwen settings
    team-up 1          # tmux session: lead + 1 grunt
    team send grunt1 --new-task --question "..." --scope src/A.cs
    team wait --task 001 --timeout 600     # background this from the lead
    team verify 001                        # re-reads every cited line
    team down          # restore .qwen/settings.json, remove the bus

Install once:

    ln -sf "$PWD/bin/team" ~/.local/bin/team
    ln -sf "$PWD/bin/team-up" ~/.local/bin/team-up

`team` and `team-up` are meant to be run from inside whatever repo they manage, not from here.

Design: `docs/superpowers/specs/2026-07-10-native-team-design.md`
