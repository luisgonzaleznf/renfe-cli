"""Terminal review before handing a booking to Renfe."""

import sys

from rich import box
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .checkout import Method


def choose(
    journey: dict, trains: list[dict], method: Method | None, review_only: bool,
) -> tuple[str, Method] | None:
    console = Console(stderr=True)
    route = f"{journey['origin']['name']} → {journey['destination']['name']}"
    console.print(Text(route, style="bold"))
    console.print(f"{journey['date']} · One way · 1 adult")

    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Train")
    table.add_column("Departs")
    table.add_column("Arrives")
    table.add_column("Changes", justify="right")
    table.add_column("From", justify="right")
    for index, train in enumerate(trains, 1):
        fares = train["fares"]
        arrival = train["arrival"][11:16]
        if train["departure"][:10] != train["arrival"][:10]:
            arrival = f"{arrival} ({train['arrival'][5:10]})"
        table.add_row(
            str(index), train["service"], train["departure"][11:16], arrival,
            str(train["changes"]), f"{min(fare['price'] for fare in fares):.2f} €",
        )
    console.print(table)
    try:
        index = 0
        if len(trains) > 1:
            choice = Prompt.ask("Train row", console=console,
                                choices=[str(index) for index in range(1, len(trains) + 1)],
                                show_choices=False)
            index = int(choice) - 1
        train = trains[index]
        console.print(f"Train {train['service']} · Published fares", style="bold")
        for fare in train["fares"]:
            console.print(Text(f"  {fare['name'] or 'Fare'}  {fare['price']:.2f} €"))

        if method is None:
            methods = [item.value for item in Method
                       if item != Method.apple_pay or (sys.platform == "darwin" and not review_only)]
            method = Method(Prompt.ask("Payment method", console=console, choices=methods,
                                       default=Method.paypal.value))
        else:
            console.print(f"Payment method · {method.value}")

        action = "Open Renfe for price review" if review_only else "Continue to Renfe"
        if Confirm.ask(action, console=console, default=True):
            return train["service"], method
    except (EOFError, KeyboardInterrupt):
        pass
    return None
