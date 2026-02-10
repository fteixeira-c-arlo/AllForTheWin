"""Menu and status displays using rich."""
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

# ASCII borders for panels and tables
ASCII_BOX = box.ASCII

# Teal color for ASCII art
ASCII_ART_COLOR = "#26D0CE"

# User-provided ASCII art for welcome page
ASCII_HUMMINGBIRD = """
                                             
                                         ==    
                                        ===    
                                      ====     
                                    .====      
                                   ====.       
                                 -====         
               .---             ====     ==    
          .============       ====-    .===    
     -========.    .====-    ====     ====     
           ======     :    ====:    -====      
              =====       -===     ====        
                ====-    .===    :====         
                  ====   ====    ===           
                   -==== ===    -===           
                     =======.   -===           
                       ======    ===.          
                        =====.    ====         
                                   ====-       
                                     ====      
                                      =====    
                                        ====.  
                                               
"""

# System command names shown in a separate section at the bottom
_SYSTEM_CMD_NAMES = frozenset({
    "help", "status", "stop_server", "server_status", "update_url", "use_local_fw",
    "config_show", "config_update", "config_delete", "tail_logs", "tail_logs_stop",
    "parse_logs", "parse_logs_stop",
    "disconnect", "exit", "back",
})


# Introduction: what this script can do (single space after bullets for even alignment)
WELCOME_INTRO = """[bold]What this terminal does:[/]
[cyan]\u2022[/] Connect to [bold]Arlo E3 Wired[/] cameras via [bold]ADB[/] (USB) or [bold]SSH[/]
[cyan]\u2022[/] Run device commands (capture, record, logs) and [bold]fw_setup[/] for firmware updates
[cyan]\u2022[/] [bold]fw_setup[/]: download FW from Artifactory, run a local server, and point the camera at it
[cyan]\u2022[/] [bold]use_local_fw[/]: use an existing FW server folder — start server and set camera update_url
[cyan]\u2022[/] Save Artifactory credentials ([bold]config_show[/] / [bold]config_update[/]) and pull logs from the camera

[bold white]Type [bold cyan]s[/] to start device selection and connection. Type [bold cyan]x[/] to close.[/]"""


def show_welcome() -> None:
    """Display welcome banner with intro to the right of ASCII art. Device selection is triggered by 's' (or 'models')."""
    title = Text.from_markup("[bold cyan]Arlo Camera Control Terminal[/] [dim]v1.0[/]")
    subtitle = Text.from_markup("[dim]Hackathon Demo - Phase 1[/]")
    intro = Text.from_markup(WELCOME_INTRO.strip())
    bird = Text(ASCII_HUMMINGBIRD, style=ASCII_ART_COLOR)
    # Two-column grid: art left, gap, intro right; intro left-aligned for clean edges
    art_width = max(len(line) for line in ASCII_HUMMINGBIRD.strip().splitlines())
    gap = 8  # space between ASCII art and welcome text
    grid = Table.grid(expand=True)
    grid.add_column(width=art_width + gap)
    grid.add_column(ratio=1, vertical="middle", justify="left")
    grid.add_row(bird, Align.left(intro))
    banner = Panel(
        Group(title, subtitle, Text(), grid),
        border_style="cyan",
        box=ASCII_BOX,
        padding=(0, 1),
    )
    console.print(banner)
    console.print()


def show_disconnected_help() -> None:
    """Show available commands when not connected."""
    console.print("[bold white]Type [bold cyan]s[/] to start device selection and connection. Type [bold cyan]x[/] to close.[/]")


def show_models_section(models: list[dict]) -> None:
    """Show the model list section (table + hint). Call this when user runs the 'models' command."""
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=ASCII_BOX,
        padding=(0, 1, 0, 1),
        show_lines=False,
    )
    table.add_column("[bold]#[/]", style="dim", width=2)
    table.add_column("[bold]Model[/]", style="cyan", width=14)
    table.add_column("[bold]Description[/]", width=52)
    for i, m in enumerate(models, 1):
        table.add_row(str(i), m["name"], m.get("display_name", "\u2014"))
    hint = Text.from_markup("[dim]Use arrows to move, type to search. Choose [bold]Exit[/] to cancel.[/]")
    content = Group(table, Text(), hint)
    panel = Panel(
        content,
        title="[bold cyan]Select camera model[/]",
        border_style="cyan",
        box=ASCII_BOX,
        padding=(0, 1, 1, 1),
    )
    console.print(panel)
    console.print()


def show_models_table(models: list[dict]) -> None:
    """Display available camera models in a table (used elsewhere if needed without panel)."""
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=ASCII_BOX,
        padding=(0, 1, 0, 1),
        show_lines=False,
    )
    table.add_column("[bold]#[/]", style="dim", width=2)
    table.add_column("[bold]Model[/]", style="cyan", width=14)
    table.add_column("[bold]Description[/]", width=52)
    for i, m in enumerate(models, 1):
        table.add_row(str(i), m["name"], m.get("display_name", "\u2014"))
    console.print("[bold]Camera models[/]")
    console.print(table)
    console.print()


def show_connection_methods() -> None:
    """Display connection method options in a compact, styled panel."""
    conn_table = Table(
        show_header=False,
        box=ASCII_BOX,
        padding=(0, 1, 0, 1),
        show_lines=False,
    )
    conn_table.add_column(style="bold cyan", width=4)
    conn_table.add_column(style="white", width=18)
    conn_table.add_column(style="dim", width=36)
    conn_table.add_row("1", "UART (serial)", "Serial console / USB–UART adapter")
    conn_table.add_row("2", "ADB (USB)", "USB debugging, shell auth")
    conn_table.add_row("3", "SSH", "Network, root login")
    conn_table.add_row("[dim]\u2022[/]", "[dim]Back[/]", "Return to model selection")
    hint = Text.from_markup("[dim]Pick an option in the list below.[/]")
    content = Group(conn_table, Text(), hint)
    panel = Panel(
        content,
        title="[bold cyan]Connection method[/]",
        border_style="cyan",
        box=ASCII_BOX,
        padding=(0, 1, 1, 1),
    )
    console.print(panel)
    console.print()


def show_commands_table(commands: list[dict], include_system: bool = True) -> None:
    """Display available commands grouped by category: compact, easy to scan."""
    device_cmds = [c for c in commands if c["name"] not in _SYSTEM_CMD_NAMES]
    system_cmds = [c for c in commands if c["name"] in _SYSTEM_CMD_NAMES]

    subtitle = " (from Confluence)" if include_system and device_cmds else ""
    console.print(Text.from_markup(f"[bold cyan]Available Commands[/][dim]{subtitle}:[/]"))

    categories_order: list[str] = []
    by_category: dict[str, list[dict]] = {}
    for c in device_cmds:
        cat = c.get("category") or "Other"
        if cat not in by_category:
            by_category[cat] = []
            categories_order.append(cat)
        by_category[cat].append(c)

    # Compact table: minimal padding, no row lines
    for cat in categories_order:
        console.print(Text.from_markup(f"[bold yellow]{cat}[/]"))
        tbl = Table(
            show_header=True,
            header_style="bold cyan",
            box=ASCII_BOX,
            padding=(0, 1, 0, 1),
            show_lines=False,
        )
        tbl.add_column("Command", style="cyan", width=14)
        tbl.add_column("Description", style="dim", width=52)
        for c in by_category[cat]:
            tbl.add_row(c["name"], c.get("description", ""))
        console.print(tbl)

    if system_cmds:
        console.print(Text.from_markup("[bold yellow]System[/]"))
        tbl = Table(
            show_header=True,
            header_style="bold cyan",
            box=ASCII_BOX,
            padding=(0, 1, 0, 1),
            show_lines=False,
        )
        tbl.add_column("Command", style="cyan", width=14)
        tbl.add_column("Description", style="dim", width=52)
        for c in system_cmds:
            tbl.add_row(c["name"], c.get("description", ""))
        console.print(tbl)

    console.print(Text.from_markup("[dim]Type [bold]help[/] to see this again.[/]\n"))


def show_connection_status(
    connection_type: str,
    device_identifier: str,
    model_name: str,
    connected_at: str | None = None,
) -> None:
    """Show current connection status."""
    table = Table(show_header=False, box=None)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_row("Connection", connection_type)
    table.add_row("Device", device_identifier)
    table.add_row("Model", model_name)
    if connected_at:
        table.add_row("Connected at", connected_at)
    console.print(table)


def show_success(message: str) -> None:
    """Print a success message with green checkmark."""
    console.print(f"[bold green]\u2713[/] [green]{message}[/]")


def show_error(message: str, suggestion: str | None = None) -> None:
    """Print an error message with red cross and optional suggestion."""
    console.print(f"[bold red]\u2717[/] [red]{message}[/]")
    if suggestion:
        console.print(f"[yellow]{suggestion}[/]")
