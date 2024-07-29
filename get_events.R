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
meetupr::meetup_auth('/srv/docker-config/meetup/httr-oauth.meetupr')
meetups <- config::get(file = '/srv/docker-config/meetup/meetups.yml')
board <- pins::board_folder('/srv/docker-pins/meetup')

groups <- meetupr::find_groups(
  'Ansible',
  topic_category_id = 546
) |>
  distinct(urlname, .keep_all = T) |>
  filter(stringr::str_to_lower(urlname) %in% stringr::str_to_lower(meetups$allowlist))

pins::pin_write(board, groups,
                name = "groups")

groups <- groups |>
  transmute(group.id = id, urlname)

possibly_get_events <- possibly(meetupr:::get_events,
                                otherwise = NA)

events <- groups |>
  rowwise() |>
  mutate(data = map(urlname, possibly_get_events)) |>
  unnest(data) |>
  drop_na(status) |>
  mutate(event_status = status)

pins::pin_write(board, events,
                name = "events")
