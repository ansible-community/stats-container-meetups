## Docker Container for Meetup Reports

This container handles the gathering of Ansible Meetup data, the generating/sending of the email report, and the updater for Discourse. Each task can be run independently (caveat that `get_events.R` has run at least once to make a dataset).

## Setup

This container requires two mount points:
- a config dir mounted to `/srv/docker-config/meetup` for the OAuth token, meetup list, and email config
- a `pins` dir mounted to `/srv/docker-pins/meetup` for storing/reading data

### Example dir layout

Inside the container it should look like this:
```
/srv/docker-config
└── meetup
    ├── email.yml
    ├── httr-oauth.meetupr
    └── meetups.yml
/srv/docker-pins
└── meetup
```

### httR OAuth file

Getting the OAuth token is a bit fiddly. In short, look at [the MeetupR docs](https://github.com/rladies/meetupr/?tab=readme-ov-file#oauth-yes) and generate one. Ask me if you get stuck. Save the file in the config mount-point as above

### Email config file

I'm assuming a Gmail account in the code, just provide a file that looks like the example in this repo.

### Meetup config file

Yaml file of white- and black-listed meetups. The code actually downloads this file directly from GitHub, so just open PRs against it.

## Build the container

```
podman build --tag meetupr .
```

## Run the container

All the tasks are designed for a single execution

### Example single run of get_events.R

This is the default action and builds a `pin` of the groups and events

```
podman run --rm -ti -v /srv/docker-config/meetup/:/srv/docker-config/meetup/ -v /srv/docker-pins/meetup:/srv/docker-pins/meetup meetupr:latest
```

### Example single run of send_email.R

This renders `meetup_report.Rmd` and emails it

```
podman run --rm -ti -v /srv/docker-config/meetup/:/srv/docker-config/meetup/ -v /srv/docker-pins/meetup:/srv/docker-pins/meetup meetupr:latest Rscript /opt/meetupr/send_email.R
```

### Example single run of the Discourse updater

TODO

### Interactive testing

You can run an R shell in the container:

```
podman run --rm -ti -v /srv/docker-config/meetup/:/srv/docker-config/meetup/ -v /srv/docker-pins/meetup:/srv/docker-pins/meetup meetupr:latest R
```
