import sys
import os
from yaml import safe_load, YAMLError
from subprocess import call, Popen, DEVNULL, PIPE
from datetime import timedelta, datetime, date, timezone
from typing import *
from dataclasses import *
from functools import partial
from unidecode import unidecode
from urllib.request import urlopen


# catch SIGINT and prevent it from terminating the script, since an instance of Ranger
# might be running and it crashes when called using subprocess. Popen (might be related
# to https://github.com/ranger/ranger/issues/898)
from signal import signal, SIGINT

signal(SIGINT, lambda _, __: None)

### DATACLASSES PRESCRIBING THE YAML SYNTAX ###
@dataclass
class Strict:
    """A class for strictly checking whether each of the dataclass variable types match."""

    def __post_init__(self):
        """Perform the check."""
        for name, field_type in self.__annotations__.items():
            value = self.__dict__[name]

            # ignore None values and Any types
            if value is None or field_type is Any:
                continue

            # go through all of the field types and check the types
            for f in (
                get_args(field_type)
                if get_origin(field_type) is Union
                else [field_type]
            ):
                if isinstance(value, f):
                    break
            else:
                raise TypeError(
                    f"The key '{name}' "
                    + f"in class {self.__class__.__name__} "
                    + f"expected '{field_type.__name__}' "
                    + f"but got '{type(value).__name__}' instead."
                )


@dataclass
class Teacher(Strict):
    name: Union[str, list]
    email: Union[str, list] = None
    website: str = None
    office: str = None
    note: str = None


@dataclass
class Classroom(Strict):
    address: str = None
    number: str = None
    floor: int = None


@dataclass
class Time(Strict):
    day: str
    start: int
    end: int
    weeks: str = None


@dataclass
class Finals(Strict):
    date: date
    classroom: Classroom


@dataclass
class Course(Strict):
    name: str
    type: str
    abbreviation: str

    teacher: Teacher = None
    time: Time = None
    classroom: Classroom = None

    website: str = None
    finals: Finals = None

    other: Any = None

    def is_ongoing(self) -> bool:
        """Returns True if the course is ongoing and False if not."""
        today = datetime.today()

        return (
            today.weekday() == self.weekday()
            and self.time.start <= today.hour * 60 + today.minute <= self.time.end
        )

    def weekday(self) -> int:
        """Get the weekday the course is on."""
        return WD_EN.index(self.time.day.lower())

    def path(self, ignore_type: bool = False) -> str:
        """Returns the path of the course (possibly ignoring the type)."""
        return os.path.join(
            *(
                [courses_folder, f"{self.name} ({self.abbreviation})"]
                + ([] if ignore_type else [self.type])
            )
        )

    def get_website_source_code(self) -> Union[str, None]:
        """Return the source code of the website of the course. If it doesn't have a
        website then throw an exception."""
        return urlopen(self.website).read().decode("utf-8")

    def website_cache_path(self) -> str:
        """Return the path to the website cache."""
        return os.path.join(self.path(), ".school")

    def update_website_cache(self):
        """Update the website source code (if it has a website)."""
        source_code = self.get_website_source_code()

        if source_code is not None:
            with open(self.website_cache_path(), "w") as f:
                f.write(source_code)

    def get_website_cache(self) -> Union[str, None]:
        """Get the cached code for the course website or None if it has not yet been
        initialized."""
        cache_path = self.website_cache_path()

        # if it doesn't yet exist, return None
        if not os.path.isfile(cache_path):
            return

        # else return its contents
        else:
            return open(cache_path, "r").read()

    @classmethod
    def from_dictionary(cls, d: Dict):
        """Initialize a Course object from the given dictionary."""
        return cls.__from_dictionary(cls, d)

    @classmethod
    def __from_dictionary(cls, c, d):
        """A helper function that converts a nested dictionary to a dataclass.
        Inspired by https://stackoverflow.com/a/54769644."""
        if is_dataclass(c):
            fieldtypes = {f.name: f.type for f in fields(c)}
            return c(**{f: cls.__from_dictionary(fieldtypes[f], d[f]) for f in d})
        else:
            return d


### GLOBAL VARIABLES ###
courses_folder = "aktuální semestr/"

# weekday constants
WD_EN = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sundnay")
WD_CZ = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")

# flags
short = False


def get_cron_schedule(time: int, day: int) -> str:
    """Returns the cron schedule expression for the specified parameters."""
    return f"{time % 60} {time // 60} * * {day + 1}"  # day + 1, since 0 == Sunday


def minutes_to_HHMM(minutes: int) -> str:
    """Converts a number of minutes to a string in the form HH:MM."""
    return f"{str(minutes // 60).rjust(2)}:{minutes % 60:02d}"


def get_ongoing_course() -> Union[Course, None]:
    """Returns the currently ongoing course (or None if there is no ongoing course)."""
    for course in get_sorted_courses(include_unscheduled=False):
        if course.is_ongoing():
            return course


def get_course_from_argument(argument: str) -> List[Course]:
    """Returns all courses that match the format name-[type] or abbreviation-[type].
    Examples: of valid identifiers (1st semester): ups-c, la, la-p, dm-c."""
    # special case for no argument at all
    if argument is None:
        ongoing = get_ongoing_course()
        return [ongoing] if ongoing is not None else get_course_from_argument("next")

    # special case for 'next'
    if argument in ("n", "next"):
        today = datetime.today()

        MID = 1440  # minutes in a day
        MIW = 10080  # minutes in a week

        current_week_time = today.weekday() * MID + today.hour * 60 + today.minute
        min_time, min_course = float("+inf"), None

        # TODO: do binary search
        for course in get_sorted_courses(include_unscheduled=False):
            time_to_course = (
                (course.time.start + course.weekday() * MID) - current_week_time
            ) % MIW

            if time_to_course < min_time:
                min_time = time_to_course
                min_course = course

        return [min_course]

    # if the argument is not 'next', try to interpret the identifier as an abbreviation
    parts = argument.lower().split("-")

    abbr = parts[0]
    type = None if len(parts) == 1 else parts[1]

    # courses that were parsed as if the argument was an abbreviation
    abbr_courses = [
        course
        for course in get_sorted_courses()
        if abbr == course.abbreviation.lower() and type in {None, course.type[0]}
    ]

    # return the courses for argument as an abbreviation or for argument as a name
    return (
        abbr_courses
        if len(abbr_courses) != 0
        else [
            course
            for course in get_sorted_courses()
            if unidecode(course.name.lower()).startswith(unidecode(abbr.lower()))
            and type in {None, course.type[0]}
        ]
    )


def get_next_course_message(i: int, courses: list) -> str:
    """Returns the string of the cron job that should be ran for the upcoming course."""
    course = (
        None
        if i + 1 >= len(courses) or courses[i].time.day != courses[i + 1].time.day
        else courses[i + 1]
    )

    return (
        "Dnes již žádný další předmět není."
        if course is None
        else (
            f"Další předmět je <i>{course.name} ({course.type})</i>, "
            f"který začíná <i>{course.time.start - courses[i].time.end} minut</i> po tomto"
            + (
                "."
                if course.classroom is None
                else (
                    f" v učebně <i>{course.classroom.number}</i>"
                    + (
                        "."
                        if course.classroom.floor is None
                        else (f" ({course.classroom.floor}. patro).")
                    )
                )
            )
        )
    )


def weekday_to_cz(day: str) -> str:
    """Converts a day in English to a day in Czech"""
    return dict(list(zip(WD_EN, WD_CZ)))[day.lower()]


def weekday_to_en(day: str) -> str:
    """Converts a day in Czech to a day in English"""
    return dict(list(zip(WD_CZ, WD_EN)))[day.lower()]


def get_sorted_courses(include_unscheduled=False) -> List[Course]:
    """Returns a list of Course dataclasses from the courses .yaml files. This method
    ignores courses without a schedule by default."""
    courses = []

    for root, _, filenames in os.walk(courses_folder):
        for filename in filter(lambda f: f.endswith(".yaml"), filenames):
            path = os.path.join(root, filename)

            with open(path, "r") as f:
                try:
                    # get the name and the type from the course folder path
                    # removes the duplicity while keeping things organized
                    shortened_path = path[len(courses_folder) :]
                    course_name = shortened_path[: shortened_path.index(os.sep)]
                    course_abbreviation = course_name[course_name.rfind(" ") :][2:-1]

                    shortened_path = shortened_path[len(course_name) + 1 :]
                    course_type = shortened_path[: shortened_path.index(os.sep)]

                    course_dict = safe_load(f) or {}
                    course_dict["name"] = course_name[: course_name.rfind(" ")]
                    course_dict["type"] = course_type
                    course_dict["abbreviation"] = course_abbreviation

                    # either it's a normal or an unscheduled course
                    if "time" in course_dict or include_unscheduled:
                        courses.append(Course.from_dictionary(course_dict))
                except (YAMLError, TypeError) as e:
                    sys.exit(f"ERROR in {path}: {e}")
                except KeyError as e:
                    sys.exit(f"ERROR in {path}: Invalid key {e}.")

    return sorted(
        courses, key=lambda c: (0, 0) if not c.time else (c.weekday(), c.time.start)
    )


def list_finals():
    """Lists dates of all finals."""
    # get courses that have finals records in them
    finals_courses = [c for c in get_sorted_courses() if c.finals is not None]
    if len(finals_courses) == 0:
        sys.exit("No finals added!")

    # build a table
    finals = [["Finals!"]]

    for course in sorted(finals_courses, key=lambda c: c.finals.date):
        final = course.finals

        delta = final.date.replace(tzinfo=None) - datetime.today()
        due_msg = "done" if delta.days < 0 else f"{delta.days + 1} days"

        finals.append(
            [
                course.name,
                final.date.strftime("%_d. %-m. %Y"),
                final.date.strftime("%_H:%M"),
                due_msg,
                str(final.classroom.number),
                "-" if final.classroom.floor is None else str(final.classroom.floor),
            ]
        )

    print_table(finals)


def list_timeline():
    """List the courses in a timeline."""
    start_hour = 9  # 9 AM
    end_hour = 21  # 20 PM (not inclusive)

    hours = end_hour - start_hour  # number of hours in a day
    beginning_minutes = start_hour * 60  # minutes from 0:00 to <start_hour>:00

    courses = get_sorted_courses(include_unscheduled=False)

    # print the header
    print("╭" + "─" * (hours * 6 + 1) + "╮", end="\n│")
    for i in range(hours):
        print(minutes_to_HHMM(beginning_minutes + 60 * i).rjust(6, " "), end="")
    print(" │\n├" + "─" * (hours * 6 + 1) + "┤\n│", end="")

    for i in range(len(courses)):
        # check for new lines
        if i != 0 and courses[i].weekday() != courses[i - 1].weekday():
            # the space padding after end of the previous course
            prev_course_padding = (end_hour * 60 - courses[i - 1].time.end) // 10
            print(" " * (prev_course_padding + 1) + "│\n│", end="")

        # the wait period between this and the previous course
        wait = (
            courses[i].time.start
            - (
                courses[i - 1].time.end
                if courses[i - 1].weekday() == courses[i].weekday()
                else beginning_minutes
            )
        ) // 10

        ongoing = (courses[i].time.end - courses[i].time.start) // 10

        print(f"{' ' * wait}({courses[i].abbreviation.center(ongoing - 2)})", end="")

    # the space padding after the very last course
    prev_course_padding = (end_hour * 60 - courses[-1].time.end) // 10
    print(" " * (prev_course_padding + 1) + "│")

    # footer
    print("╰" + "─" * (hours * 6 + 1) + "╯")


def list_attribute(argument, attribute=""):
    "List the given course attribute."
    courses = get_course_from_argument(argument)

    if len(courses) == 0:
        sys.exit("No course matching the criteria.")
    elif len(courses) != 1:
        sys.exit("Multiple courses matching the identifier.")
    else:
        # print the whole course if the attribute was not specified
        if attribute == "":
            print(courses[0])

        # else only the specific attribute
        else:
            print(
                "The course does not contain this attribute."
                if not hasattr(courses[0], attribute)
                else getattr(courses[0], attribute)
            )


def list_courses(option=""):
    """Lists information about the courses."""
    courses = get_sorted_courses()

    current_day = datetime.today()
    current_weekday = current_day.weekday()

    # split to scheduled and non-scheduled
    unscheduled = [c for c in courses if c.time is None]
    courses = [c for c in courses if c not in unscheduled]

    table = []
    for i, course in enumerate(courses):
        # lambda functions to test for various options
        # a is current weekday and b is the course's weekday
        options = {
            "": lambda a, b: True,  # all of them
            "t": lambda a, b: a == b,  # today
            "tm": lambda a, b: (a + 1) % 7 == b,  # tomorrow
            "mo": lambda a, b: b == 0,
            "tu": lambda a, b: b == 1,
            "we": lambda a, b: b == 2,
            "th": lambda a, b: b == 3,
            "fr": lambda a, b: b == 4,
            "sa": lambda a, b: b == 5,
            "su": lambda a, b: b == 6,
        }

        if option not in options:
            sys.exit("Invalid option!")

        if options[option](current_weekday, course.weekday()):
            # include the name of the day before first day's course
            if courses[i - 1].time.day != courses[i].time.day:
                weekday = weekday_to_cz(courses[i].time.day).capitalize()

                # calculate the next occurrence
                date = (
                    current_day
                    + timedelta(days=(course.weekday() - current_weekday) % 7)
                ).strftime("%-d. %-m.")

                table.append([f"{weekday} / {date}"])

            # for possibly surrounding the name with chars if it's ongoing
            name_surround_char = "•" if course.is_ongoing() else ""

            # append useful information
            table.append(
                [
                    f"{name_surround_char}{course.name if not short else course.abbreviation}{name_surround_char}",
                    "-" if course.type is None else course.type[0],
                    f"{minutes_to_HHMM(courses[i].time.start)} - {minutes_to_HHMM(courses[i].time.end)}"
                    + ("" if course.time.weeks is None else f" ({course.time.weeks})"),
                    "-" if course.classroom is None else course.classroom.number,
                ]
            )

    # list unscheduled courses only when no options are specified
    if option == "" and len(unscheduled) != 0:
        table.append(["Nerozvrženo"])
        for course in unscheduled:
            table.append(
                [
                    course.name if not short else course.abbreviation,
                    course.type[0],
                    "-",
                    "-",
                ]
            )

    # if no courses were added since the days didn't match, exit with a message
    if len(table) == 0:
        sys.exit("No courses matching the criteria found!")

    print_table(table)


def print_table(table: List[List[str]]):
    # find max width of each of the columns of the table
    column_widths = [0] * max(len(row) for row in table)
    for row in table:
        # skip weekday rows
        if len(row) != 1:
            for i, entry in enumerate(row):
                if column_widths[i] < len(entry):
                    column_widths[i] = len(entry)

    for i, row in enumerate(table):
        print(end="╭─" if i == 0 else "│ ")

        column_sep = " │ "
        max_row_width = sum(column_widths) + len(column_sep) * (len(column_widths) - 1)

        # if only one item is in the row, it's the weekday and is printed specially
        if len(row) == 1:
            print(
                (f"{' ' * max_row_width} │\n├─" if i != 0 else "")
                + f"◀ {row[0]} ▶".center(max_row_width, "─")
                + ("─╮" if i == 0 else "─┤")
            )
        else:
            for j, entry in enumerate(row):
                print(
                    entry.ljust(column_widths[j])
                    + (column_sep if j != (len(row) - 1) else " │\n"),
                    end="",
                )

    print(f"╰{'─' * (max_row_width + 2)}╯")


def open_in_vim(path: str):
    """Opens the specified path in Vim."""
    call(["vim", path])


def open_in_ranger(path: str):
    """Opens the specified path in Ranger."""
    call(["ranger", path])


def open_in_firefox(url: str):
    """Opens the specified website in FireFox."""
    Popen(["firefox", "-new-window", url])


def open_in_xournalpp(path: str):
    """Opens the specified Xournal++ file in Xournal++."""
    # suppress the warnings, since Xournal++ talks way too much
    Popen(["xournalpp", path], stdout=DEVNULL, stderr=DEVNULL)


def check_all_websites():
    """Update caches of all course websites."""
    print("Updating course website caches:")

    for course in get_sorted_courses():
        if course.website is not None:
            print(f"- updating {course.name} ({course.type})...")
            course.update_website_cache()
        else:
            print(f"- skipping {course.name} ({course.type}) -- no website")


def check_course_website(argument: Union[str, None] = None):
    """Checks, whether the source code of the website matches the one cached. If yes,
    let the user know. If not, print the diff and update the cache."""
    courses = get_course_from_argument(argument)

    if len(courses) == 0:
        sys.exit("No course matching the criteria.")

    elif len(courses) > 1 and not all(
        [courses[i].website == courses[i + 1].website for i in range(len(courses) - 1)]
    ):
        sys.exit("Multiple courses matching the identifier.")

    elif courses[0].website is None:
        sys.exit("The course has no website.")

    # get the current and the cached source code
    cached_code = courses[0].get_website_cache()
    current_code = courses[0].get_website_source_code()

    # update the cache
    courses[0].update_website_cache()
    if cached_code is None:
        sys.exit("Cache file created.")
    else:
        # if there hasn't been any changes, say so
        if cached_code == current_code:
            # get time modified in a readable format
            mtime = os.path.getmtime(courses[0].website_cache_path())
            mtime_string = datetime.fromtimestamp(mtime).strftime("%-d. %-m. %Y")

            sys.exit(f"No updates since {mtime_string}.")

        # else print the changes diff
        else:
            Popen(
                [f"diff '{courses[0].website_cache_path()}' - -u --color=always"],
                stdin=PIPE,
                shell=True,
            ).communicate(current_code.encode())


def open_course(kind: str, argument: Union[str, None] = None):
    """Open the course's something."""
    # if no argument is specified, default to getting the current or the next course
    courses = get_course_from_argument(argument)

    # if none were found
    if len(courses) == 0:
        sys.exit(f"No course matching the criteria.")

    # if one was found
    elif len(courses) == 1:
        course = courses[0]

        if kind == "website":
            if course.website is not None:
                open_in_firefox(course.website)
            else:
                sys.exit("The course has no website.")

        elif kind == "folder":
            open_in_ranger(course.path())

        elif kind == "notes":
            path = os.path.join(course.path(), "notes.xopp")

            # check if the default notes exist
            if not os.path.isfile(path):
                sys.exit(f"The course has no notes.")
            else:
                open_in_xournalpp(path)

    # if multiple were found
    else:
        # if multiple courses were found and they're all the same
        if kind == "folder" and all(
            [
                courses[i].abbreviation == courses[i + 1].abbreviation
                for i in range(len(courses) - 1)
            ]
        ):
            open_in_ranger(courses[0].path(ignore_type=True))

        # if multiple courses were found and the websites match
        elif kind == "website" and all(
            [
                courses[i].website == courses[i + 1].website
                for i in range(len(courses) - 1)
            ]
        ):
            open_in_firefox(courses[0].website)
        else:
            sys.exit(f"Multiple courses matching the identifier.")


def compile_notes() -> None:
    """Runs md_to_pdf script on all of the courses."""
    base = os.path.dirname(os.path.realpath(__file__))

    for path in map(lambda c: c.path(), get_sorted_courses()):
        os.chdir(os.path.join(base, path))
        call(["fish", "-c", "md_to_pdf -a -t"])


def compile_cron_jobs() -> None:
    """Adds notifications for upcoming classes to the crontab file."""
    # check if the script is running as root; if not, call itself as root
    if not os.geteuid() == 0:
        call(["sudo", "python3", *sys.argv])
        sys.exit()

    courses = get_sorted_courses(include_unscheduled=False)

    cron_file = "/etc/crontab"
    user = os.getlogin()

    # comments to encapsulate the generated cron jobs
    cron_file_comments = {
        "beginning": "# BEGINNING: course schedule crons (autogenerated, do not change)",
        "end": "# END: course schedule crons",
    }

    with open(cron_file, "r+") as f:
        contents = f.readlines()
        f.seek(0)

        # write to file till we reach the end or the comment section is skipped, so we
        # can add the new course-related cron jobs
        i = 0
        while i < len(contents):
            if contents[i].strip() == cron_file_comments["beginning"]:
                while contents[i].strip() != cron_file_comments["end"]:
                    i += 1

                i += 1
                break
            else:
                f.write(contents[i])

            i += 1

        f.write(cron_file_comments["beginning"] + "\n")

        for j, course in enumerate(courses):
            # the messages regarding the course
            messages = [
                (
                    get_cron_schedule(course.time.end - 5, course.weekday()),
                    get_next_course_message(j, courses),
                ),
                (
                    get_cron_schedule(course.time.start, course.weekday()),
                    f"právě začal předmět <i>{course.name} ({course.type})</i>.",
                ),
            ]

            for cron_schedule, body in messages:
                f.write(f"{cron_schedule} {user} dunstify rozvrh '{body}'\n")

        f.write(cron_file_comments["end"] + "\n")

        # write the rest of the file
        while i < len(contents):
            f.write(contents[i])
            i += 1

        # cut whatever is left
        f.truncate()

        print(f"Course messages generated and saved to {cron_file}.")


def list_help(tree: Dict, indentation: int) -> None:
    """Recursively pretty-prints a nested dictionary (with lists being functions)."""
    for k, v in tree.items():
        decision = "  " * indentation + f"{{{', '.join(k)}}}"

        # either it's a function with annotation
        if type(v) is not dict:
            print(decision.ljust(30) + v[1])

        # or it's simply a decision
        else:
            print(decision)
            list_help(v, indentation + 1)


# change path to current folder for the script to work
os.chdir(os.path.dirname(os.path.realpath(__file__)))

decision_tree = {
    ("list",): {
        ("courses",): (list_courses, "List information about the courses."),
        ("attribute",): (list_attribute, "List the given course attribute."),
        ("finals",): (list_finals, "List dates of all finals."),
        ("timeline",): (list_timeline, "List the courses in a timeline."),
    },
    ("compile",): {
        ("cron",): (compile_cron_jobs, "Add crontab notifications for all courses.",),
        ("notes",): (compile_notes, "Run md_to_pdf script on all course notes."),
    },
    ("config",): (
        partial(open_in_ranger, os.path.dirname(os.path.abspath(__file__))),
        "Open the course directory in Ranger.",
    ),
    ("check",): {
        ("all",): (check_all_websites, "Update all website caches."),
        ("course",): (
            check_course_website,
            "Check, whether the course website has changed.",
        ),
    },
    ("open",): {
        ("folder", "course"): (
            partial(open_course, "folder"),
            "Open the course's folder in Ranger.",
        ),
        ("website",): (
            partial(open_course, "website"),
            "Open the course's website in FireFox.",
        ),
        ("notes",): (
            partial(open_course, "notes"),
            "Open the course's notes in Xournal++.",
        ),
        ("script",): (partial(open_in_vim, sys.argv[0]), "Open this script in Vim."),
    },
}

arguments = sys.argv[1:]

# parse flags
i = 0
while i < len(arguments):
    if not arguments[i].startswith("-"):
        i += 1
        continue

    # TODO: use reflection to do this automatically?
    argument = arguments.pop(i)
    if argument in ("--short", "-s"):
        short = True

    i += 1


# if no arguments are specified, list help
if len(arguments) == 0:
    print(
        "A multi-purpose script for simplifying my MFF UK education.\n"
        + "\n"
        + "supported options:"
    )

    list_help(decision_tree, 1)

    print(
        "\nsupported supported flags:\n"
        + "  --short/-s".ljust(30)
        + "Shorten names to abbreviations (when possible)."
    )

    sys.exit()

# go down the decision tree
parsed_arguments = []
while len(arguments) != 0 and type(decision_tree) is dict:
    argument = arguments.pop(0)

    # sort the decisions by their common prefix with the argument
    decisions = sorted(
        (max([len(argument) if s.startswith(argument) else 0 for s in d]), d)
        for d in decision_tree
    )

    # if no match is found, quit with error
    if decisions[-1][0] == 0:
        sys.exit(
            f"ERROR: '{argument}' doesn't match decisions in the decision tree: {{{', '.join(' or '.join(d) for d in decision_tree)}}}"
        )

    # filter out the sub-optimal decisions
    decisions = list(filter(lambda x: x[0] == decisions[-1][0], decisions))

    # if there are multiple optimal solutions, the command is ambiguous
    if len(decisions) > 1:
        sys.exit(
            f"ERROR: Ambiguous decisions for '{argument}': {{{', '.join(' or '.join(d) for _, d in decisions)}}}",
        )
    else:
        parsed_arguments.append(decisions[0][1])
        decision_tree = decision_tree[decisions[0][1]]

# if the decision tree isn't a function by now, exit; else extract the function
if type(decision_tree) is dict:
    sys.exit(
        f"ERROR: Decisions remaining: {{{', '.join(' or '.join(d) for d in decision_tree)}}}"
    )

try:
    decision_tree[0](*arguments)
except TypeError:
    print(
        f"Invalid arguments for '{', '.join([' '.join(a) for a in parsed_arguments])}'."
    )
