# qbit-quick

qbit-quick is a library of functions for qBittorrent to help with manging torrents.
The primary function was initially to ensure you are one of the first in a swarm when a new torrent becomes available,
although it's expanded to provide other useful functions as well now.\
One of the main features is that it can be run in server mode. This is especially useful if you want to run it in a
docker container, allowing other apps to interact with via HTTP calls.
**NOTE:** I wrote this script to work with torrentleech. There's no reason it shouldn't work with other trackers, but
it's not been tested with them.

## What is racing?

Racing is important for private trackers where you are required to maintain an upload/download ratio. These trackers
provide a means for you to be updated when a new torrent becomes available, so being part of the initial swarm gives
you a much better chance of obtaining a positive ratio.\
The update mechanism is usually via an IRC bot, which can then be forwarded onto your *arr apps, such as autobrr, sonarr
or radarr. These in turn will pass the torrent onto qBittorrent, which will then download it. So to get the best ratio,
you want to download the torrent as soon as possible so you can start uploading it to others.

### So why do I need this?

The problem is that the IRC channel updates when a new torrent is added, but if you grab it immediately, it may not
have been indexed by the tracker yet. This means when qBittorrent tries to download it, the tracker states that the
torrent doesn't exist or hasn't been registered.\
After multiple failed retries, qBittorrent will then wait 30 minutes before trying again and by that time, you've
missed out the initial swarm.

The `race` function in this library continually retries until the torrent succeeds or one of the preconfigured limits
is breached. It also provides additional means to improve your initial download speed, such as pausing other torrents,
while the race is happening and then resuming them again once the download is complete.

## Installation

Download the packaged wheel to the host where qBittorrent is running.

```bash
  pip install qbit_quick-1.0.0-py3-none-any.whl
```

## Usage

The first time you run `qbit-quick race`, it will create a default `config.json` in your user's config directory (see
[plaformdirs.user_config_dir](https://platformdirs.readthedocs.io/en/latest/api.html#user-config-directory)). You can
override this location by setting the `QBQ_CONFIG_DIR` environment variable.

### Example config

```json
{
  "qbittorrent": {
    "host": "127.0.0.1",
    "port": 8080,
    "username": "admin",
    "password": "adminadmin"
  },
  "ignore_categories": ["ignore"],
  "racing": {
    "race_categories": ["race"],
    "pausing": {
      "ratio": 1.0
    },
    "max_reannounce": 100,
    "reannounce_frequency": 5.0
  },
  "pausing": {
    "time_since_active": "1d",
    "time_active": "1w"
  },
  "debug_logging": false
}
```

* `qbittorrent`
  * `host`:`str` - URL or hostname where your qBittorrent instance is running
  * `port`:`int` - Port number if using hostname:port instead of a URL
  * `username`:`str` - Username if authentication is enabled
  * `password`:`str` - Password if authentication is enabled

* `ignore_categories`:`list[str]` - Any torrents with a category matching one in this list, will be ignored by this
  script, meaning they won't be eligible for pausing or racing.
* `racing` - This section contains the settings used for racing torrents
  * `race_categories`:`list[str]` - Only torrents with a category set to one in this list will be eligible for racing.
    If not set, all torrents will be eligible for racing (except if they're in the ignore list).
  * `pausing` - This section contains the criteria under which other torrents should be paused before racing begins.
    * `ratio`:`float` - Only torrents with a share ratio below what is set here will be eligible for pausing when
      another torrent starts racing.
  * `max_reannounce`:`int` - The maximum number of times to reannounce before giving up. If not set, the script will
    reannounce indefinitely until the torrent starts successfully.
  * `reannounce_frequency`:`float` - The number of seconds to wait between each reannounce. Defaults to 5.0s if not set.
* `pausing` - This section contains the criteria for which torrents should be paused by the `pause` command.
  * `time_since_active`: `duration` - The duration since the torrent was last active. If `last activity > 
    time_since_last_active`, then the torrent is eligible to be paused. This duration is in the format `1w2d3h4m5s`,
    where `1w = 1 week`, `2d = 2 days`, `3h = 3 hours`, `4m = 4 minutes` and `5s = 5 seconds`.
  * `time_active` - The duration that this torrent has been active for. If the `total active time > time_active`, then
    the torrent is eligible to be paused. This duration is in the format `1w2d3h4m5s`, where `1w = 1 week`,
    `2d = 2 days`, `3h = 3 hours`, `4m = 4 minutes` and `5s = 5 seconds`.
* `debug_logging`:`bool` - Set to `true` to enable debug level logging. Defaults to `false` if not set.

### Reannouncing doesn't always work

Sometimes reannouncing isn't what's required to get a torrent started. There are a few messages that trackers can return
which require the torrent to be stopped and restarted instead. There's no documentation on this list, so this is purely
based on which messages I've seen.

* `unregistered` - This means the torrent hasn't been registered with the tracker yet.
* `stream truncated` - This likely means the connection was closed for some reason.
* `too many requests` - If you set the reannounce frequency too high, you might get blocked by the tracker. This is only
  temporary, so the script is set to wait 10 seconds before trying again.

### qBittorrent setup

1. In qBittorrent go to `Tools` -> `Options` -> Select the `Downloads` tab.
2. Scroll down to the `Run external program` section
3. Check the box next to `Run on torrent added`
4. Enter your command which will be something like:
    * `/home/user/.local/bin/qbit-quick race %I` - The `%I` passes in the hash of the torrent being added
    * Alternatively, you may want to point it to a bash script instead so you can set environment variables or log the
      output to a file. For example:
      ```bash
      #!/bin/bash
      export QBQ_LOGS_DIR="/home/user/qbit-quick/logs"
      export QBQ_CONFIG_DIR="/home/user/qbit-quick/config"
      export QBQ_STATE_DIR="/home/user/qbit-quick/state"
      /home/user/.local/bin/qbit-quick race "$1" >> ~/qbit-quick/logs/qbit-quick.log 2>&1
      ```
5. Check the box next to `Run on torrent finished`
6. Enter your command which will be something like:
    * `/home/user/.local/bin/qbit-quick post-race "%I"` - The `%I` passes in the hash of the torrent that has finished
    * Alternatively, you may want to point it to a bash script instead so you can set environment variables or log the
      output to a file. For example:
      ```bash
      #!/bin/bash
      export QBQ_LOGS_DIR="/home/user/qbit-quick/logs"
      export QBQ_CONFIG_DIR="/home/user/qbit-quick/config"
      export QBQ_STATE_DIR="/home/user/qbit-quick/state"
      /home/user/.local/bin/qbit-quick post-race "$1" >> ~/qbit-quick/logs/qbit-quick.log 2>&1
      ```

### CLI Commands

<a id="race"/>`race <torrent_hash>`

When a torrent starts racing, it starts by pausing all other torrents that match the criteria (assuming pausing is
enabled). This list of paused torrents is stored in a SQLite database so that they can be resumed afterwards.
The script then continually restarts the torrent or reannounces to the tracker until at least 1 of the trackers is
connected successfully or until it's reaches one of the preconfigured limits.

<a id="post-race"/>`post-race <torrent_hash>`

Once a torrent is finished racing, it resumes all the torrents that were paused before it started racing. However, it
will not resume a torrent if another race is in progress that would have also paused that one. For example:

1. Torrent A starts racing and pauses Torrents X and Y. Torrent Z is not paused as it's still downloading. Torrent P is
   already in a paused state, but not due to any other racing torrents, it's been paused manually.
2. Torrent B starts racing before Torrent A has finished, but after Torrent Z has finished, so it just pauses
   Torrent Z, as Torrents X and Y are already paused. They would have been paused though if Torrent A hadn't already
   paused them. This is different to Torrent P, which was not paused by Torrent A.
3. Torrent A finishes, but it doesn't unpause any torrents, as Torrent B is still in progress and all 3 torrents,
   X, Y and Z would have also been paused by Torrent B had they not already been paused.
4. Torrent B finishes and unpauses Torrents X, Y and Z. Torrent P remains paused.

<a id="pause"/>`pause --id <id>`

Pauses all torrents that match the criteria defined in the config. This is useful to allow the files to be
moved, as that can't happen while the files are in use. The optional `--id <id>` argument allows you to set a custom id,
e.g. `"mover"`, as this value is required to call `unpause`. If you don't provide an id, a default id of `"pause"` is
used.

<a id="unpause"/>`unpause --id <id>`

Unpauses all eligible torrents that were paused using the pause command. The optional `--id <id>` argument allows you
to pass in the id used when pausing the torrents. If you don't provide an id, a default id of `"pause"` is used.

<a id="info"/>`info --status <status> --field <field_name> --include-field-names --format <format>`

Retrieves information about the current torrents. If no arguments are provided, then all available information is
returned for all the torrents. This information can be filtered using the following arguments:
* `--status` - Filter by status e.g. all, downloading, seeding. See `info --help` for the full list of accepted values.
* `--field` - The list of fields to include in the response. Can either be specified as multiple `--field` or `-f`
values e.g. `--field name --field size` or `-f name -f size` or as a comma-delimited list of values e.g.
`--field name,size` or `-f name,size`.
See [here](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)#torrent-management) for the full
list of accepted values.
* `--include-field-name` - Whether to include the field names in the output. Defaults to `False` if not set.
* `--format` - Specifies what format to return information in. Can be either `json` or `plain` where plain means
comma-delimited plain text.

<a id="config-print"/>`config --print`

Prints out the current config.

`config --edit`

Opens the current config in the default editor.

`db --print`

Prints out the contents of the SQLite database in a tabulated ascii format.

`db --clear`

Clears all entries from the SQLite database. This could be useful if it's got into a bad/messy state for some reason.

`db --delete torrent_hash`

Deletes the specified torrent has from the SQLite database.

`server`

Starts qbit-quick in server mode. This is useful if you want to run qbit-quick in a docker container, as it allows the
app to remain running and commands can be triggered via the HTTP interface.\
It's important to note that some of the commands run asynchronously, and although these commands can be cancelled using
their associated `task_id`, this is not a hard termination. It is a request for the command to stop gracefully at the
next appropriate point.

### HTTP Endpoints

Assuming the server is running on 127.0.0.1 and is running on port 8081, these would be the accessible endpoints:

`[GET] http://127.0.0.1:8081/`

Returns a list of all available endpoints.

`[POST] http://127.0.0.1:8081/race/<torrent_hash>`

See: [race](#race) for more details.\
Returns a JSON message containing a `task_id`, as this command is asynchronous.
The `task_id` can be used to track or cancel the associated task.

`[POST] http://127.0.0.1:8081/post-race/<torrent_hash>`

See: [post-race](#post-race) for more details.\
This task runs synchronously, as there would be no appropriate point to interrupt it (this is because all the torrent
hashes are passed directly to qbittorrent in a single API call).

`[POST] http://127.0.0.1:8081/pause` \
`[POST] http://127.0.0.1:8081/pause/<id>`

See: [pause](#pause) for more details.\
This task runs synchronously, as there would be no appropriate point to interrupt it (this is because all the torrent
hashes are passed directly to qbittorrent in a single API call).

`[POST] http://127.0.0.1:8081/unpause` \
`[POST] http://127.0.0.1:8081/unpause/<id>`

See: [unpause](#unpause) for more details.\
This task runs synchronously, as there would be no appropriate point to interrupt it (this is because all the torrent
hashes are passed directly to qbittorrent in a single API call).

`[GET] http://127.0.0.1:8081/info` \
`[GET] http://127.0.0.1:8081/info?status=<status>&fields=<field1>&fields=<field2>&format=<format>&include_field_names=<true|false>`

See: [info](#info) for more details.\
Returns information about the current torrents. The filters can be applied by providing them as HTTP request parameters.

`[DELETE|POST] http://127.0.0.1:8081/cancel/<task_id>`

Requests the task associated with the given `task_id` to terminate at the next appropriate point (this does not just
kill the running process).

`[GET] http://127.0.0.1:8081/tasks`

Returns a list of all currently running tasks e.g. all torrents currently racing along with their task_id, which can be
used to cancel them.

`[GET] http://127.0.0.1:8081/config`

Returns the current JSON config.

`[POST|PUT] http://127.0.0.1:8081/config`

Saves the provided JSON config as the current config, if it is valid.

`[GET] http://127.0.0.1:8081/db`

Returns the current SQLite database as an HTML table.

`[DELETE] http://127.0.0.1:8081/db/<torrent_hash>`

Deletes the specified entry from the SQLite database.

`[DELETE] http://127.0.0.1:8081/db`

Deletes all entries from the SQLite database.


### Custom file location

You can override the default config directory by setting the `QBQ_CONFIG_DIR` environment variable. This is where
qbit-quick will look for the config.json file.

```bash
  export QBQ_CONFIG_DIR="/home/user/qbit-quick/config"
```

You can override the default logs directory by setting the `QBQ_LOGS_DIR` environment variable. This is where qbit-quick
will write its log files to.

```bash
  export QBQ_LOGS_DIR="/home/user/qbit-quick/logs"
```

You can override the default state directory by setting the `QBQ_STATE_DIR` environment variable. This is where
qbit-quick will write its sqlite db, which is used to store the state of which torrents were paused, so it knows which
ones to unpause again when the `post-race` command is run.

```bash
  export QBQ_STATE_DIR="/home/user/qbit-quick/state"
```

## Building

```bash
  python3 -m venv .venv
  source .venv/bin/activate
  pip install poetry
  poetry install
```
