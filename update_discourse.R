library(tidyverse)
library(glue)
library(pins)
library(httr)

board <- pins::board_folder('/srv/docker-pins/meetup')

## Create events in the Discourse API

# Helper function to do the work in a pipeline
push_to_discourse <- function(e) {

  config <- config::get(file = '/srv/docker-config/meetup/email.yml')

  # Production Forum
  url      = config$discourse$url
  category = config$discourse$category
  api_key  = config$discourse$api_key
  api_user = config$discourse$api_user

  auth     =   httr::add_headers(
    "Api-Key" = api_key,
    "Api-Username" = api_user
  )

  # Get current meetup events on forum
  search_str = "#events tags:meetup status:open @MeetupBot"
  r <- httr::GET(
    url = stringr::str_c(
      url,
      "/search.json?expanded=true&q=",
      curl::curl_escape(search_str)
    ),
    config = auth,
    encode = 'json'
  ) |> content()

  ids <- map_int(r$topics, 'id')|> tibble::enframe(name = NULL, value =  "topic_id")

  get_external_ids <- possibly(function(id, auth) {
    r <- httr::GET(
      stringr::str_c(url, "/t/", id, "/1.json"),
      config = auth,
      encode = 'json'
    ) |> content()

    return(
      tibble(
        post_id   = r$post_stream$posts[[1]]$id,
        meetup_id = r$external_id
      )
    )
  }, otherwise = NA)

  existing_meetups <- if (nrow(ids) == 0) {
    tibble(topic_id = 0, post_id = 0, meetup_id = "a", .rows = 0)
  } else {
    ids |>
      mutate(data = map(topic_id, get_external_ids, auth = auth)) |>
      unnest(cols = 'data')
  }

  # helper
  build_body <- function(urlname, eventname, event_url, date_time, description) {
    title <- stringr::str_c(urlname, ": ", eventname, " [", as.Date(date_time), "]")
    raw <- "[event "
    raw <- stringr::str_c(raw, "url='", event_url, "' ")
    raw <- stringr::str_c(raw, "start='", as.Date(date_time), "' ")
    raw <- stringr::str_c(raw, "status='public' ")
    raw <- stringr::str_c(raw, "]\n[/event]")
    raw <- stringr::str_c(raw, "\n", description)

    return(list(title = title, raw = raw))
  }

  ## PUT updates to existing events
  update_event <- function(post_id, topic_id, urlname, eventname, event_url, date_time, description) {
    r     <- build_body(urlname, eventname, event_url, date_time, description)
    title <- r$title
    raw   <- r$raw

    httr::PUT(
      url   = stringr::str_c(url, '/t/', topic_id, '.json'),
      config = auth,
      encode = 'json',
      body = list(
        title = title,
        category = category
      )
    )

    httr::PUT(
      url   = stringr::str_c(url, '/posts/', post_id, '.json'),
      config = auth,
      encode = 'json',
      body = list(post = list(
        raw = raw,
        edit_reason = "meetupbot api update"
      ))
    )

    return(stringr::str_c(url,'/t/',topic_id,'/1'))
  }

  ## Post new events
  post_event <- function(urlname, eventname, event_url, date_time, id, description) {
    r     <- build_body(urlname, eventname, event_url, date_time, description)
    title <- r$title
    raw   <- r$raw

    post_str = glue("title={title}&raw={raw}&external_id={id}&tags=[meetup]&category={category}")

    r <- httr::POST(
      url   = stringr::str_c(url, '/posts'),
      config = auth,
      encode = 'json',
      query = list(
        title = title,
        raw = raw,
        external_id = id,
        `tags[]` = "meetup",
        category = category
      )
    ) |> content()

    if (is.null(r$topic_id)) {
      return("Failed")
    } else {
      return(stringr::str_c(url,'/t/',r$topic_id,'/1'))
    }
  }

  right_join(e, existing_meetups, by = c("id" = "meetup_id")) |>
    na.omit() |>
    mutate(result = pmap_chr(
      list(post_id = post_id, topic_id = topic_id,
           urlname = urlname, eventname = title,
           event_url = event_url, date_time = date_time, description = description),
      update_event)) -> updated

  ## POST events that don't exist
  anti_join(e, existing_meetups, by = c("id" = "meetup_id")) |>
    mutate(result = pmap_chr(
      list(urlname = urlname, eventname = title,
           event_url = event_url, id = id, date_time = date_time, description = description),
      post_event)) -> new

  return(
    rbind(
      updated |> select(-topic_id, -post_id),
      new))
}

groups <- pin_read(board, "groups")
events <- pin_read(board, "events") |>
  tidyr::drop_na(id) |>
  left_join(select(groups,urlname,country), by = 'urlname') |>
  mutate(is_online_event = venues_venue_type == 'online' | venues_name == 'Online event',
         Location = country)

events |>
  distinct(id, .keep_all = T) |>
  filter(date_time > Sys.Date() - lubridate::days(1)) |>
  filter(date_time <= Sys.Date() + 90) |>
  filter(status == 'ACTIVE') |>
  push_to_discourse() |>
  arrange(date_time) %>%
  transmute(Event = title,
            date_time = as.Date(date_time),
            Location,
            Group = urlname,
            rsvps_total_count,
            result)
