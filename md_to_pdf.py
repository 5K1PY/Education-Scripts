import os, shutil, glob  # folder + path utilities
from subprocess import Popen, PIPE  # executing shell commands
from re import sub, compile, MULTILINE  # for cropping
import random  # generating random strings for cache
import argparse  # command line interaction


def run_shell_command(command: [str], ignore_errors=False):
    """Run a shell command. If stderr is not empty, the function will terminate the
    script (unless specified otherwise) and print the error message."""
    _, stderr = map(
        lambda b: b.decode("utf-8").strip(),
        Popen(command, stdout=PIPE, stderr=PIPE).communicate(),
    )

    if not ignore_errors and stderr != "":
        exit(f"\n{command[0].capitalize()} error:\n| " + stderr.replace("\n", "\n| "))


def generate_random_hex_number(length: int):
    """Generates a random hexadecimal number (with possible leading zeroes!) of the
    specified length."""
    return "".join(random.choice("0123456789abcdef") for _ in range(length))


def xopp_to_svg(input_file: str, output_file: str):
    """Convert a .xopp file to a .svg file using Xournal++. Note that xournalpp errors
    are ignored by default, since stderr produces warnings."""
    run_shell_command(
        ["xournalpp", f"--create-img={output_file}", input_file], ignore_errors=True
    )


def svg_to_pdf(input_file: str, output_file: str):
    """Convert a .svg file to a .pdf file using InkScape."""
    run_shell_command(
        ["inkscape", "-C", "-z", f"--file={input_file}", f"--export-pdf={output_file}"]
    )


def md_to_pdf(input_file: str, output_file: str, parameters: [str]):
    """Convert a .md file to a .pdf file using Pandoc."""
    run_shell_command(["pandoc", input_file, "-o", output_file, *parameters])


def crop_svg_file(file_name: str, margin: float = 0):
    """Crop the specified .svg file.
    TODO: add support for cropping files that include text."""

    with open(file_name, "r") as svg_file:
        contents = svg_file.read()

        # set the default values for the coordinates we're trying to find
        inf = float("inf")
        min_x, min_y, max_x, max_y = inf, inf, -inf, -inf

        # find all paths and their respective descriptions
        paths = compile(r'<path(.+?)d="(.+?)"', MULTILINE).finditer(contents)
        next(paths)  # skip the first one, which is always a solid color background

        for path in paths:
            coordinate_parts = path.group(2).strip().split(" ")
            m_count, l_count = coordinate_parts.count("M"), coordinate_parts.count("L")

            # ignore the paper grid coordinates (alternating m/l commands) and don't
            # ignore pen strokes (since they're one m and one l command)
            if m_count == l_count and m_count + l_count > 2:
                continue

            # get only the coordinate numbers
            coordinates = [float(c) for c in coordinate_parts if not c.isalpha()]

            # check for min/max
            for x, y in zip(coordinates[::2], coordinates[1::2]):
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)

        # adjust for margins
        min_x -= margin
        min_y -= margin
        max_x += margin
        max_y += margin

        # add/update svg values
        substitutions = (
            (r'<svg(.*)width="(.+?)pt', f'<svg\\1width="{max_x - min_x}pt'),  # width
            (r'<svg(.*)height="(.+?)pt', f'<svg\\1height="{max_y - min_y}pt'),  # height
            (r"<svg(.+)>", f'<svg\\1 x="{min_x}" y="{min_y}">'),  # min x and y
            (
                r'<svg(.*)viewBox="(.*?)"(.*)>',
                f'<svg\\1viewBox="{min_x} {min_y} {max_x - min_x} {max_y - min_y}"\\3>',
            ),  # viewbox
        )

        for pattern, replacement in substitutions:
            contents = sub(pattern, replacement, contents)

    # overwrite the file
    with open(file_name, "w") as svg_file:
        svg_file.write(contents)


def delete_files(files: [str]):
    """Deletes all of the files specified in the list."""
    for f in files:
        os.remove(f)


def get_argument_parser():
    """Returns the ArgumentParser object for the script."""
    parser = argparse.ArgumentParser(
        description="Convert markdown files with embedded Xournal++ files to pdf.",
        epilog="\n  ".join(
            [
                "examples:",
                "py md_to_pdf.py -a                               | convert all .md files",
                "py md_to_pdf.py -s -f README.md                  | silently convert README.md",
                "py md_to_pdf.py -a -p='--template=eisvogel.tex'  | convert all .md files using a pandoc template",
            ]
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    # clean-up after the script
    parser.add_argument(
        "-c",
        "--no-cleanup",
        dest="cleanup",
        action="store_false",
        help="don't cleanup cache files generated by the script",
    )

    # supress Xournal++ file embedding
    parser.add_argument(
        "-x",
        "--no-xopp-files",
        dest="embed_xopp_files",
        action="store_false",
        help="don't embed Xournal++ files",
    )

    # silent
    parser.add_argument(
        "-s",
        "--silent",
        dest="silent",
        action="store_true",
        help="prevent the script from outputting any messages",
    )

    # svg margins
    parser.add_argument(
        "-m",
        "--margins",
        dest="margins",
        metavar="M",
        type=int,
        default=15,
        help="set the margins around the cropped Xournal++ files (in points, default 15)",
    )

    # pandoc parameters
    parser.add_argument(
        "-p",
        "--pandoc-parameters",
        dest="pandoc_parameters",
        metavar="P",
        default=[],
        nargs=argparse.REMAINDER,
        help="specify pandoc parameter(s) used in the conversion",
    )

    group = parser.add_mutually_exclusive_group(required=True)

    # all .md files
    group.add_argument(
        "-a",
        "--all-files",
        dest="files",
        default=[],
        action="store_const",
        const=glob.glob("*.md"),
        help="convert all Markdown files in the current directory",
    )

    # only specific files
    group.add_argument(
        "-f",
        "--files",
        dest="files",
        default=[],
        metavar="F",
        nargs="+",
        help="the name(s) of the markdown file(s) to be converted to pdf",
    )

    return parser


# get the parser and parse the commands
parser = get_argument_parser()
arguments = parser.parse_args()


def print_message(*args):
    """A print() wrapper that does nothing when the silent argument is specified."""
    if not arguments.silent:
        print(*args)


# make note of the generated files to remove them after the conversions
generated_files = []

# go through the specified markdown files
for md_file_name in arguments.files:

    # read the md file
    with open(md_file_name, "r") as f:
        print_message(f"Reading {md_file_name}:")
        contents = f.read()

        if arguments.embed_xopp_files:
            # find each of the .xopp files in the .md file
            for match in compile(r"\[(.*)]\((.+?).xopp\)", MULTILINE).finditer(
                contents
            ):
                file_label, file_name = match.groups()

                # convert the .xopp file to .svg file(s)
                print_message(f"- converting {file_name}.xopp to SVG...")
                xopp_to_svg(f"{file_name}.xopp", f"{file_name}.svg")

                # get all .svg files generated from the .xopp file
                file_names = [f[:-4] for f in glob.glob(f"{file_name}*.svg")]

                # covert the .svg files to .pdf, cropping them in the process
                for file_name in file_names:
                    print_message("- cropping SVG...")
                    crop_svg_file(f"{file_name}.svg", arguments.margins)

                    print_message(f"- converting {file_name}.svg to PDF...")
                    svg_to_pdf(f"{file_name}.svg", f"{file_name}.pdf")

                    generated_files += [f"{file_name}.svg", f"{file_name}.pdf"]

                # replace the links to the .xopp files to the .pdf images
                contents = contents.replace(
                    match.group(0),
                    "\n\n".join(
                        [
                            f"![{file_label}]({file_name}.pdf)"
                            for file_name in file_names
                        ]
                    ),
                )

    print_message("- generating resulting PDF...")

    # create a dummy .md file for the conversion
    dummy_file_name = generate_random_hex_number(10) + ".md"
    with open(dummy_file_name, "w") as f:
        f.write(contents)

    # convert the .md file to .pdf
    md_to_pdf(dummy_file_name, md_file_name[:-2] + "pdf", arguments.pandoc_parameters)

    generated_files += [dummy_file_name]

    print_message()

# clean-up after the script is done
if arguments.cleanup:
    print_message("Cleaning up...")
    delete_files(generated_files)

print_message("Done!\n")
