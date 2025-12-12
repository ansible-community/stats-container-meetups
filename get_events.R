library(meetupr)
library(dplyr)
library(tidyr)
library(purrr)
library(pins)

## Attempt to update the meetup list from github
url  <- "https://github.com/ansible-community/stats-container-meetups/raw/main/meetups.yml"
req  <- httr2::request(url) |> httr2::req_retry(max_tries = 5)
resp <- httr2::req_perform(req)
if (httr2::resp_status(resp) == 200) {
  resp |>
    httr2::resp_body_string() |>
    readr::write_file('/srv/docker-config/meetup/meetups.yml')
}

# Loaded from the config mount-point
source('/srv/docker-config/meetup/meetup.env')
meetup_ci_load()

meetups <- config::get(file = '/srv/docker-config/meetup/meetups.yml')
board <- pins::board_folder('/srv/docker-pins/meetup')

groups <- meetupr::get_pro_groups('Ansible') |>
  distinct(urlname, .keep_all = T) |>
  filter(stringr::str_to_lower(urlname) %in% stringr::str_to_lower(meetups$allowlist))

pins::pin_write(board, groups,
                name = "groups")

groups <- groups |>
  transmute(group.id = id, urlname)

possibly_get_events <- possibly(meetupr:::get_group_events,
                                otherwise = NA)

# We want this format for events:
# > readr::read_rds('/tmp/events.rds') |> names()
#  [1] "group.id"      "urlname"       "id"            "title"
#  [5] "link"          "status"        "time"          "duration"
#  [9] "going"         "waiting"       "description"   "venue_id"
# [13] "venue_lat"     "venue_lon"     "venue_name"    "venue_address"
# [17] "venue_city"    "venue_state"   "venue_zip"     "venue_country"
# [21] "event_status"

events <- groups |>
  rowwise() |>
  mutate(data = map(urlname, possibly_get_events)) |>
  unnest(data) |>
  mutate(event_status = status,
         time = date_time,
         going = rsvps_count,
         link = event_url,
         venue_name = venues_name)

pins::pin_write(board, events,
                name = "events")
