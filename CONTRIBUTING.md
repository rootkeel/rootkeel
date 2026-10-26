# Contributing to Rootkeel

Thank you for helping. Bug reports, documentation fixes and code are all welcome.

## Before you start

- **Security issues** go through private reporting, never a public issue: see
  [SECURITY.md](SECURITY.md).
- **Bugs**: open an issue with the version, what you ran, what you expected and what happened.
  Leave out credentials, enrollment codes, `machine.json` and grant URLs with their tokens.
- **Larger changes**: open an issue first to agree on the approach. Rootkeel deliberately stays
  small: one owner, one small server stack, plain Zellij sessions.

## Development

The code is Python 3.13 with aiohttp and cryptography, plus Bash for the scripts. Run the unit tests
from the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements/broker.txt
cd tests/unit && PYTHONPATH=../.. ../../.venv/bin/python -m unittest discover -v
```

A few process-isolation tests need root in a disposable Linux container and are skipped otherwise.
CI also runs ShellCheck, `reuse lint`, the image builds, the end-to-end test and a secret scan
(gitleaks) on every pull request. The end-to-end test (`tests/e2e/run.sh`) builds the images and runs a whole
server, four machines (two of them installed with `agent/install.sh` on clean Debian 13 and
Ubuntu 24.04) and the owner's `rt` in containers; run it as root on a Linux host with Docker that
you can spare for about ten minutes: `sudo tests/e2e/run.sh`. Pull requests that change the release
machinery also get a dry run of the release workflow, which builds every release download without
publishing anything.

Guidelines:

- Keep changes focused, and add or update tests for any behaviour you change.
- New source files start with an SPDX licence header comment naming `AGPL-3.0-only`, like the existing
  files.
- Pin every new dependency to an exact version and a checksum or digest, and add it to
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) with its licence.
- Never log credentials, cookies, terminal input or output.
- Write commit messages and pull request titles as plain one-line summaries of the change.

## Contributor licence agreement

Before we can merge your first pull request, you need to agree to the contributor licence agreement
below. A bot comments on your pull request with a link; signing takes a minute and covers all your
future contributions. If you contribute on behalf of your employer, make sure you are allowed to,
or ask us about a corporate agreement.

### Individual Contributor Licence Agreement

This agreement is between you ("You") and the Rootkeel project ("We" or "Us"), and applies to
every Contribution You submit to any repository of the Rootkeel project.

1. **Definitions.** "Contribution" means any work of authorship, including source code,
   documentation and other material, that You intentionally submit to Us for inclusion in Rootkeel,
   for example through a pull request, a patch or an issue comment that contains code. "Submit"
   means any form of communication sent to Us or Our repositories, issue trackers or discussion
   forums for the purpose of discussing or improving Rootkeel.

2. **Copyright licence.** You grant Us and the recipients of software distributed by Us a
   perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable copyright licence to
   reproduce, prepare derivative works of, publicly display, publicly perform, sublicense and
   distribute Your Contributions and such derivative works, under any licence terms, including
   proprietary terms. This licence lets Us distribute Rootkeel under the GNU Affero General Public
   License and also under other terms.

3. **Patent licence.** You grant Us and the recipients of software distributed by Us a perpetual,
   worldwide, non-exclusive, no-charge, royalty-free, irrevocable (except as stated in this
   section) patent licence to make, have made, use, offer to sell, sell, import and otherwise
   transfer Your Contributions, where the licence applies only to those patent claims licensable
   by You that are necessarily infringed by Your Contribution alone or by its combination with
   Rootkeel. If any entity institutes patent litigation against You or any other entity alleging
   that Your Contribution, or Rootkeel, constitutes patent infringement, any patent licence granted
   to that entity under this agreement for that Contribution terminates as of the date such
   litigation is filed.

4. **Your rights stay yours.** You keep the copyright in Your Contributions. Apart from the licences
   above, You may use Your Contributions in any way You like.

5. **Your representations.** You represent that You are legally entitled to grant these licences;
   that each Contribution is Your original creation, or that You identify in it the third-party
   material it contains together with that material's licence and any restrictions You know of;
   and that, if Your employer has rights to intellectual property You create, You have permission
   to make the Contribution on its behalf or Your employer has waived those rights for it.

6. **Notice.** You agree to tell Us if You become aware of any fact or circumstance that would make
   these representations inaccurate in any respect.

7. **No warranty, no obligation.** Unless required by applicable law or agreed in writing, You
   provide Your Contributions "as is", without warranties or conditions of any kind. We are not
   obliged to use or include any Contribution.

8. **Acceptance.** You accept this agreement by stating so in the way the pull request bot asks,
   for example by commenting on Your pull request with the sentence it gives You.
