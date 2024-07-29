FROM rocker/tidyverse:latest

RUN install2.r config emayili gt patchwork pins remotes \
    && rm -rf /tmp/downloaded_packages
RUN R -q -e 'remotes::install_github("rladies/meetupr")'

RUN mkdir -p /opt/meetupr
WORKDIR /opt/meetupr
COPY ./get_events.R .
COPY ./send_email.R .
COPY ./update_discourse.R .
COPY ./meetup_report.Rmd .

CMD ["R"]
