# get the config
config <- config::get(file = '/srv/docker-config/meetup/email.yml')
date <- Sys.Date()

# build the email

msg <- emayili::envelope() |>
  emayili::from("gsutclif+ds@redhat.com") |>
  emayili::to(config$email_targets) |>
  emayili::subject(paste0("Meetup report - ", date)) |>
  emayili::render('meetup_report.Rmd',
                  include_css = c("rmd", "bootstrap"))

smtp <- emayili::gmail(
  username = config$email_config$username,
  password = config$email_config$password
)

smtp(msg, verbose = TRUE)
