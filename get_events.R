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
meetupr::meetup_ci_load()

meetups <- config::get(file = '/srv/docker-config/meetup/meetups.yml')
board <- pins::board_folder('/srv/docker-pins/meetup')

groups <- meetupr::get_pro_groups('Ansible') |>
  distinct(urlname, .keep_all = T) |>
  filter(stringr::str_to_lower(urlname) %in% stringr::str_to_lower(meetups$allowlist))

pins::pin_write(board, groups,
                name = "groups")

groups <- groups |>
  transmute(group.id = id, urlname)

possibly_get_events <- possibly(meetupr::get_group_events,
                                otherwise = NA)

events <- groups |>
  rowwise() |>
  mutate(events_data = map(urlname, possibly_get_events)) |>
  unnest(events_data, keep_empty = TRUE) |>
  filter(!is.na(id))

pins::pin_write(board, events,
                name = "events")
