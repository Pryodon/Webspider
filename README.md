# Webspider — Cross-platform Python 3 edition

Webspider crawls websites and directory indexes, imports sitemap trees, checks
media and other files, generates verified sitemaps, and retains persistent
SQLite crawl history. It can resume interrupted work and conditionally recrawl
sites later.

The ordinary `urls` output can be passed to
[GNU Wget](https://www.gnu.org/software/wget/) when the discovered files should
also be downloaded.

## Requirements

Webspider uses only the Python 3 standard library. It does not require `pip`, a
virtual environment, `wget`, Bash, or GNU command-line tools. It runs on Linux,
Windows, and macOS.

## Installation

```bash
# Clone or copy the script into your PATH
git clone https://github.com/Pryodon/Webspider.git
cd Webspider
chmod +x webspider
# optional: symlink as 'spider'
ln -s "$PWD/webspider.py" ~/bin/spider
```
Put this script in your PATH for ease of use!  
(e.g. put it in `~/bin` and have `~/bin` in your PATH.)

On Windows make a folder on your computer somewhere for apps and 
[put that folder in your path](https://www.architectryan.com/2018/03/17/add-to-the-path-on-windows-10/).
Then copy `webspider.py` and `webspider.cmd` into that folder. `webspider.cmd` is a script that launches webspider.

## Quick start

Linux or macOS:

```bash
python3 webspider.py --video https://www.cdn.ay1.net/pub/00-READ-ME.html
```

Windows Command Prompt or PowerShell:

```powershell
py -3 webspider.py --video https://www.cdn.ay1.net/pub/00-READ-ME.html
```

The optional `webspider.cmd` launcher may also be used on Windows:

```powershell
webspider.cmd --video https://www.cdn.ay1.net/pub/00-READ-ME.html
```

The default mode is `--video`, so this also works:

```bash
python3 webspider.py https://www.cdn.ay1.net/pub/00-READ-ME.html
```

## Repository files

- `webspider.py` — main cross-platform crawler
- `webspider.cmd` — optional Windows launcher
- `README.md` — usage and feature documentation
- `LICENSE.md` — GNU Affero General Public License version 3
- `ADDITIONAL-DISCLAIMER.md` — supplemental warranty and liability terms

## Inputs, modes, and filtering

A seed may be an HTTP, HTTPS, or—when `--follow-ftp` is enabled—FTP URL.
Multiple seeds may be supplied directly or read from a text file containing one
seed per nonblank, non-comment line. An argument naming an existing local file
is treated as a UTF-8 seed file; other arguments are treated as URLs.

Schemeless seeds use HTTPS by default. Use `--http` or `--https` to select the
default scheme explicitly:

```bash
python3 webspider.py --http --video example.com/media/
```

Each seed establishes a saved path boundary. A directory seed ending in `/`
uses that directory as its boundary; a page or file seed uses its containing
directory. Same-host links above or beside that boundary remain out of scope.

Exactly one output mode may be selected:

- `--video` — video and subtitle extensions
- `--audio` — audio extensions
- `--images` — image extensions
- `--pages` — directories and common page extensions
- `--files` — non-page files
- `--all` — every permitted discovered URL

Override a mode’s extension set with `--ext`:

```bash
python3 webspider.py --files --ext "pdf|epub|zip" https://example.com/
```

Important general controls include:

```text
--http | --https
--level N|inf
--delay SECONDS
--timeout SECONDS
--max-page-bytes BYTES
--status-200
--verbose
--log FILE
--out FILE | --output FILE | -o FILE
--version
```

`--level 0` prevents following HTML links beyond seed work; sitemap-listed
URLs may still be imported unless sitemap handling is disabled. `--level inf`
removes the HTML-link depth limit and is the default. `--delay` defaults to 0.5
seconds between requests, `--timeout` defaults to 30 seconds per request, and
`--max-page-bytes` defaults to 10,000,000 bytes per downloaded page body.

The default human-readable log is `log`, and the default URL output is `urls`.
Without `--status-200`, the URL output may include matching URLs that were
checked but returned HTTP errors such as 404 or 503. Use `--status-200` to keep
only successful 2xx checks. Verified text and XML sitemaps always use only
successful URLs.

The complete and authoritative switch reference is always available through:

```bash
python3 webspider.py --help
```

## Persistent crawl state

Every crawl uses a persistent SQLite database in the current directory. It is
retained after successful completion, Ctrl-C, network errors, and later
recrawls. The database—not the text log—is used to generate the `urls` file.

Choose a name:

```bash
python3 webspider.py --state media.sqlite3 --video https://example.com/media/
```

Without `--state`, Webspider generates a stable hidden filename such as:

```text
.webspider-state-example.com-42db7c18c1.sqlite3
```

SQLite stores seeds, scopes, pending queues, sitemap relationships, HTTP
statuses, redirects, ETags, Last-Modified values, Content-Length, Content-Type,
body hashes for downloaded pages/sitemaps, first/last seen times, change runs,
and completed or interrupted run history. WAL journaling and small transactions
provide crash-safe progress.

## Logging, verbose progress, and interruption

The text log is an incremental human-readable audit trail. SQLite—not the log—is
the source of truth for current crawl state and normal URL-list generation.

With `-v` or `--verbose`, Webspider reports URL checks and sitemap work in real
time, including sitemap phase starts and ends, queued documents, downloads,
parse summaries, imported-URL milestones, errors, robots delays, and temporary
storage cleanup.

Pressing Ctrl-C:

- records the interruption;
- preserves pending queues and validators in SQLite;
- writes the currently selected partial URL output;
- avoids publishing a partially generated XML sitemap;
- prints the exact `--resume` command; and
- exits with status 130.

`--parse-only --log FILE` remains available for older Webspider or wget-style
logs, but persistent SQLite state is preferred for all new crawls.

## Resume an interrupted crawl

```bash
python3 webspider.py --resume media.sqlite3
```

If the previous activity was within 10 minutes, Webspider normally continues
immediately. If it is older, robots.txt and sitemap roots are conditionally
refreshed.

```bash
python3 webspider.py --resume media.sqlite3 --refresh-sitemaps
python3 webspider.py --resume media.sqlite3 --no-refresh-sitemaps
python3 webspider.py --resume media.sqlite3 --sitemap-max-age 30m
```

Conditional sitemap requests use stored `ETag` and `Last-Modified` headers. A
`304 Not Modified` response avoids downloading or reparsing the sitemap. Changed
sitemaps are parsed, new child sitemaps and URLs are merged, and completed file
checks are preserved.

## Repeat a crawl later

Start a new conditional run using the same database:

```bash
python3 webspider.py --recrawl media.sqlite3
```

The recrawl default is `--changes-only`, so `urls` contains newly discovered,
modified, or restored matching files from that run.

```bash
python3 webspider.py --recrawl media.sqlite3 --changes-only
python3 webspider.py --recrawl media.sqlite3 --new-only
python3 webspider.py --recrawl media.sqlite3 --changed-only
python3 webspider.py --recrawl media.sqlite3 --gone-only
python3 webspider.py --recrawl media.sqlite3 --all-known
```

Recheck everything or only older records:

```bash
python3 webspider.py --recrawl media.sqlite3 --recheck-all
python3 webspider.py --recrawl media.sqlite3 --recheck-older-than 7d
```

## Efficient URL validation

HTML pages, robots.txt, and sitemaps use conditional `GET`. Media and other
non-page files use conditional `HEAD` first. If a server rejects `HEAD` with
403, 405, or 501, Webspider falls back to conditional `GET` with:

```text
Range: bytes=0-0
```

This checks whether a file exists or changed without downloading the whole
file. Servers returning no validators are compared using status, redirect,
Content-Length, and Content-Type; downloaded HTML and sitemap bodies also use
SHA-256 hashes.

A server may answer the one-byte fallback with HTTP 206 Partial Content.
Webspider records this method as `GET Range` and treats any successful 2xx
response as available for `--status-200` filtering and verified sitemap
generation. When `Content-Range` contains the full resource size, that total is
stored instead of the one-byte response length.

## External links and externally hosted media

Webspider remains restricted to the original seed hosts and saved path scopes
unless external crawling is explicitly enabled.

To check external media links that are directly listed by pages you are already
crawling, without following external HTML pages:

```bash
python3 webspider.py --video --external-media https://nyx.mynetblog.com/ptv/index_wayback_rewritten.html
```

`--external-media` applies to matching non-page URLs. In video mode, an
external `.mp4`, `.mkv`, `.webm`, or other configured video extension is
validated and can appear in `urls`, while an unrelated external HTML page is
not crawled solely because of this option.

To permit external HTML crawling, set a depth:

```bash
python3 webspider.py --video --external-depth 1 https://example.com/index.html
```

External depth is counted after leaving the original seed hosts:

- depth 0: original-site behavior only, which is the default;
- depth 1: direct off-site links from an original page;
- depth 2: links found on an external page at depth 1;
- depth 3: one additional off-site link hop, and so on.

The two options can be combined:

```bash
python3 webspider.py --video --external-media --external-depth 2 https://example.com/index.html
```

This follows external HTML pages through depth 2 and also validates matching
media directly found on every page that was permitted to load.

External file validation uses the same efficient conditional `HEAD` request and
one-byte conditional `GET` fallback as other non-page files.

Webspider follows robots rules independently for every origin. Before accessing
an external origin it conditionally fetches that origin's `robots.txt`, saves
its validators and body in SQLite, and applies the matching user-agent group's:

- `Allow`
- `Disallow`
- `Crawl-delay`
- `Request-rate`

The rules also apply to sitemap downloads and to both requests in the
`HEAD`-then-one-byte-`GET` fallback. Request timing is tracked separately for
each origin. The strictest of the command-line `--delay`, `Crawl-delay`, and
`Request-rate` controls the next request.

For conflicting `Allow` and `Disallow` records, Webspider uses the most
specific matching path. `Allow` wins only when the matching rules have equal
specificity. Wildcards and the terminal `$` end anchor are supported.

`--no-robots` disables these rules only for the original saved seed origins.
External HTTP, HTTPS, and FTP origins always enforce their own robots policy;
there is no external-site opt-out.

On a resume within the same run, the saved policy is reused. A later recrawl
conditionally revalidates each encountered origin's `robots.txt` before using
that origin again.

A same-host URL outside the original path scope remains excluded. For example,
a seed under `/ptv/` does not gain access to `/private/` merely because external
crawling was enabled.

The external settings are saved in the persistent SQLite database. Supply them
when starting a new state. A resume or recrawl reuses the saved values and
rejects conflicting command-line replacements.

## FTP links

FTP crawling is disabled by default. Enable it explicitly:

```bash
python3 webspider.py --video --follow-ftp https://example.com/index.html
```

With `--follow-ftp`, Webspider can:

- recognize `ftp://` links in HTML and sitemap entries;
- validate matching FTP files with metadata commands such as `MLST`, `SIZE`,
  and `MDTM` without downloading the complete media body;
- list a directly linked FTP directory;
- crawl deeper FTP directories when permitted by `--external-depth`;
- retain FTP size, modification time, availability, directory-listing hashes,
  and crawl history in the persistent SQLite database.

A directly linked FTP directory is external depth 1. Its child files can be
validated with `--follow-ftp`; a nested child directory requires
`--external-depth 2`, and so forth.

```bash
python3 webspider.py --video --follow-ftp --external-depth 3 https://example.com/index.html
```

Use `--max-ftp-entries N` to limit the number of entries accepted from one
directory listing. The default is 100,000.

FTP has no standardized robots protocol. As a conservative extension,
Webspider checks `/robots.txt` at the FTP root and honors matching
`User-agent`, `Allow`, `Disallow`, `Crawl-delay`, and `Request-rate` directives
when present. These rules are always enforced for an externally reached FTP
origin. `--no-robots` can bypass them only when the FTP origin was an original
seed.

FTP URLs may appear as entries inside an HTTP/HTTPS sitemap and are imported
when `--follow-ftp` is enabled. Sitemap documents themselves must be fetched
over HTTP or HTTPS; an FTP URL is not accepted as `--sitemap-source`.

Anonymous FTP is used when no username is present. Credentials embedded in an
FTP URL are supported, but the complete URL is retained in SQLite and the log.

## Directory-index loop protection

File servers frequently use an automatically generated directory listing
instead of an index page. Such listings may:

- expose `sitemap-index.xml` and numbered sitemap files;
- link back to the directory itself;
- contain sorting links such as `?C=N;O=D` that normalize to the same URL.

Webspider schedules each ordinary URL and each sitemap **at most once per crawl
run**. Rediscovering the root directory or an already processed sitemap updates
its database history without returning it to the current run's pending queue.
This prevents the root → sitemap phase → root loop that can otherwise occur on
an autoindexed file server.

This does not prevent a later `--recrawl` from checking the same URL or sitemap
again, because a recrawl uses a new run ID.

## Sitemap behavior

Sitemap discovery is enabled by default. Webspider reads `Sitemap:` entries
from robots.txt, tries `/sitemap.xml` when none are declared, follows nested
indexes, supports XML, text, and `.xml.gz`, and extracts standard `<loc>`,
`video:content_loc`, and `image:loc` URLs.

Use only sitemap-listed matching URLs:

```bash
python3 webspider.py --sitemap-only --video https://example.com/
```

Disable sitemap handling:

```bash
python3 webspider.py --no-sitemaps --video https://example.com/media/
```

Add an explicit nonstandard sitemap:

```bash
python3 webspider.py --sitemap-source https://example.com/maps/media.xml https://example.com/media/
```

`--sitemap-source` may be repeated. A new crawl may also use explicit sitemap
sources without separate seed arguments; their HTTP or HTTPS origin roots then
establish the initial crawl scopes.

Bound sitemap imports when working with an unfamiliar or very large site:

```text
--max-sitemap-documents N   default: 10000
--max-sitemap-depth N       default: 20
--max-sitemap-mib N         default: 64 per downloaded/decompressed document
--max-sitemap-urls N        default: 0 (unlimited)
```

Downloaded sitemap files remain temporary and are removed after normal
completion or a handled Ctrl-C. A hard process termination can leave a
`.webspider-sitemaps-*` directory for later inspection or manual cleanup. The
persistent SQLite database remains. Conventional sitemap detection does not
mistake `make-sitemaps.py.txt` for a sitemap.

Webspider can also generate verified output sitemaps:

```bash
python3 webspider.py --video --sitemap-txt https://example.com/
python3 webspider.py --pages --sitemap-xml https://example.com/
```

Related controls include:

```text
--sitemap-output FILE
--sitemap-max-urls N
--sitemap-base-url URL
```

Generated text and XML sitemaps contain only successfully verified URLs.
`--sitemap-txt` writes `sitemap.txt` beside the selected `--out` file.
`--sitemap-output` names the XML output, and `--sitemap-max-urls` controls how
many URLs may appear in one XML document (default 10,000; allowed range 1 to
50,000). Large XML outputs are split into numbered sitemap files plus a sitemap
index. `--sitemap-base-url` supplies the public base URL used for child-file
locations in that index. URLs are percent-encoded and XML-escaped, and XML
output is parsed again before Webspider reports success.

## Current sitemap membership

Webspider keeps historical URLs even when a later sitemap removes them. Use:

```bash
python3 webspider.py --recrawl media.sqlite3 --current-sitemap-only
```

to limit non-page scheduling and output to URLs currently listed by a sitemap
reachable from the current sitemap roots.

## Database output without crawling

```bash
python3 webspider.py --export-state media.sqlite3 --all-known --out videos.txt
python3 webspider.py --state-info media.sqlite3
```

The old `--parse-only --log` feature remains for pre-database log files, but
new crawls should use the database.

## State safety

A `.lock` file prevents two Webspider processes from using one database. A
handled Ctrl-C removes the lock normally. A hard process or system termination
may leave the lock, SQLite WAL/SHM sidecars, an in-flight queue record, and a
temporary sitemap directory. SQLite retains committed progress, and the next
successful state open returns in-flight queue records to pending.

If a process crashed, first confirm that no Webspider process is using the
database. Only then may the stale lock be removed as part of the requested
state operation:

```bash
python3 webspider.py --resume media.sqlite3 --force-unlock
```

Do not use `--force-unlock` merely because a crawl is taking a long time; two
writers using one database can corrupt or invalidate the crawl state.

Start over while preserving the old database as a timestamped backup:

```bash
python3 webspider.py --state media.sqlite3 --fresh https://example.com/media/
```

`--fresh` archives the existing database and its WAL/SHM sidecars beside the
original path before creating a new state. It cannot be combined with resume,
recrawl, export, state-info, or deletion operations.

Delete persistent state only when explicitly requested:

```bash
python3 webspider.py --delete-state media.sqlite3
```

Deletion removes the database and its WAL, SHM, and lock sidecars. It refuses
to proceed when a lock exists unless `--force-unlock` was also explicitly
supplied after confirming that no process is active.

## TLS and security controls

Normal HTTPS certificate verification is enabled by default.

`--insecure` disables certificate verification for all HTTPS requests and
should be used only when the risk is understood.

`--insecure-ip-https` disables verification only for HTTPS URLs whose host is a
literal IPv4 or IPv6 address; normal hostnames remain verified.

Webspider follows robots rules by default. `--no-robots` applies only to the
original saved seed origins. Every externally reached HTTP, HTTPS, or FTP origin
still enforces its own robots policy, including `Allow`, `Disallow`,
`Crawl-delay`, and `Request-rate`.

FTP credentials embedded in URLs are retained in SQLite and logs. Avoid putting
sensitive credentials in URLs unless that persistence is acceptable.

## Help

Running Webspider incorrectly prints a short basic guide. The complete built-in
manual includes all switches, conflicts, sitemap limits, and examples:

```bash
python3 webspider.py --help
```

Print only the installed Webspider version with:

```bash
python3 webspider.py --version
```

## Updating

```bash
python3 webspider.py --update
```

The updater validates the official GitHub source, creates a timestamped backup,
and atomically replaces the exact script file being run. It does not update the
README, launcher, license files, or any other repository file.

## Downloading the `urls` file with GNU Wget

Webspider discovers and validates URLs, but it does not download the files in
its `urls` output. [GNU Wget](https://www.gnu.org/software/wget/) can read that
file and download HTTP, HTTPS, and FTP URLs non-interactively.

### Install Wget

Debian, Ubuntu, or Raspberry Pi OS:

```bash
sudo apt update
sudo apt install wget
```

Fedora or Red Hat-based systems:

```bash
sudo dnf install wget
```

Arch Linux or an Arch-based distribution:

```bash
sudo pacman -S wget
```

macOS with [Homebrew](https://brew.sh/):

```bash
brew install wget
```

Windows does not normally include GNU Wget. One current option is
[MSYS2](https://www.msys2.org/):

1. Install MSYS2 and open its **UCRT64** terminal.
2. Update MSYS2:

   ```bash
   pacman -Syu
   ```

   Close and reopen the UCRT64 terminal if the updater tells you to, then run
   `pacman -Syu` again.

3. Install GNU Wget:

   ```bash
   pacman -S mingw-w64-ucrt-x86_64-wget
   ```

Run Wget from the UCRT64 terminal, or add this directory to the Windows `PATH`:

```text
C:\msys64\ucrt64\bin
```

Then verify the installation:

```text
wget --version
```

In Windows PowerShell, use `wget.exe` explicitly so that a PowerShell alias
named `wget` cannot be mistaken for GNU Wget:

```powershell
wget.exe --version
```

People who already use Chocolatey may instead install its
[community Wget package](https://community.chocolatey.org/packages/wget),
which can lag current GNU Wget releases:

```powershell
choco install wget
```

### Download the URLs

Run this command from the directory where the downloaded directory tree should
be created, with Webspider's `urls` file in that directory:

```bash
wget --no-host-directories --force-directories --no-clobber --cut-dirs=0 -i urls
```

On Windows PowerShell, use the executable name explicitly:

```powershell
wget.exe --no-host-directories --force-directories --no-clobber --cut-dirs=0 -i urls
```

The options mean:

- `--no-host-directories` — do not create a top-level directory named after
  each server;
- `--force-directories` — create and preserve the directories from each URL
  path;
- `--no-clobber` — skip a local file that already exists instead of replacing
  it or creating a numbered duplicate;
- `--cut-dirs=0` — remove zero directory components, preserving the complete
  URL path below the server name; and
- `-i urls` — read one URL per line from the `urls` file.

For example, a URL ending in `/videos/movies/file.mp4` is saved as:

```text
videos/movies/file.mp4
```

The server hostname is omitted, existing files are left untouched, and the
remote path structure is retained.

When `urls` contains more than one host, `--no-host-directories` can map two
different remote URLs to the same local path. With `--no-clobber`, the first
download wins and later collisions are skipped. Omit `--no-host-directories`
when separate host directories are needed.

## License, warranty, and liability

Copyright (C) 2026 Landon Hendee

Webspider is licensed under the
[GNU Affero General Public License version 3 or later](LICENSE.md).

SPDX-License-Identifier: `AGPL-3.0-or-later`

Webspider is provided **“AS IS”** and **“WITH ALL FAULTS,”** without warranty
of any kind. The GNU Affero General Public License contains its standard
disclaimer of warranty and limitation of liability in sections 15 and 16, as
interpreted by section 17.

Additional warranty and liability terms permitted by AGPLv3 section 7(a) are
provided in [ADDITIONAL-DISCLAIMER.md](ADDITIONAL-DISCLAIMER.md).

Each user is solely responsible for their own acts and omissions, for
determining and complying with any laws independently applicable to them, and
for evaluating whether and how to operate the software safely in their
environment. This statement allocates responsibility and does not limit the
permissions granted by the GNU Affero General Public License.

Nothing in the license or additional disclaimer excludes or limits liability
that cannot lawfully be excluded or limited under applicable law.
