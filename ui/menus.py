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

# Teal color for ASCII art (welcome page)
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
    "parse_logs", "parse_logs_stop", "parse_log_file", "export_logs_tftp",
    "disconnect", "exit", "back",
})


# Introduction: what this terminal does (single space after bullets for even alignment)
WELCOME_INTRO = """[bold]Arlo Camera Control Terminal[/]

[bold]Connect[/] — Choose [bold]UART[/], [bold]ADB[/] (USB), or [bold]SSH[/]. Device model, FW, and env are auto-detected from [bold]build_info[/] and [bold]kvcmd[/].

[bold]Device commands[/] — Loaded from Confluence (E3 Wired). Run [bold]help[/] when connected to see the full list (e.g. [bold]reboot[/], [bold]kvcmd[/], [bold]migrate[/], [bold]ptz[/]).

[bold]Firmware[/] — [bold]fw_setup[/] downloads from Artifactory, runs a local server, and sets the camera [bold]update_url[/]. [bold]use_local_fw[/] uses an existing FW server folder.

[bold]Logs[/] — [bold]tail_logs[/] / [bold]parse_logs[/] stream and parse live. [bold]parse_log_file[/] parses a log file from the [bold]arlo_logs[/bold] folder. [bold]export_logs_tftp[/] (UART) uploads logs via TFTP.

[bold]Config[/] — [bold]config_show[/] / [bold]config_update[/] for Artifactory credentials.

[bold white]Type [bold cyan]s[/] to connect. Type [bold cyan]x[/] to close.[/]"""


def show_welcome() -> None:
    """Display welcome banner with intro to the right of ASCII art. Connection is triggered by 's' (or 'connect')."""
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
    console.print("[bold white]Type [bold cyan]s[/] to connect (UART, ADB, or SSH). Type [bold cyan]x[/] to close.[/]")


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


def _build_commands_tables_renderable(
    commands: list[dict], include_system: bool = True, box_style: str | None = "ascii"
) -> Group:
    """Build commands grouped by category as a single renderable (Group of tables).
    box_style: 'ascii' for bordered tables, None for no borders (cleaner, more visible).
    """
    device_cmds = [c for c in commands if c["name"] not in _SYSTEM_CMD_NAMES]
    system_cmds = [c for c in commands if c["name"] in _SYSTEM_CMD_NAMES]

    categories_order: list[str] = []
    by_category: dict[str, list[dict]] = {}
    for c in device_cmds:
        cat = c.get("category") or "Other"
        if cat not in by_category:
            by_category[cat] = []
            categories_order.append(cat)
        by_category[cat].append(c)

    table_box = ASCII_BOX if box_style else None
    parts: list = []
    for cat in categories_order:
        parts.append(Text.from_markup(f"[bold yellow]{cat}[/]"))
        tbl = Table(
            show_header=True,
            header_style="bold cyan",
            box=table_box,
            padding=(0, 1, 0, 1),
            show_lines=False,
        )
        tbl.add_column("Command", style="cyan", width=18)
        tbl.add_column("Description", style="dim", width=60)
        for c in by_category[cat]:
            tbl.add_row(c["name"], c.get("description", ""))
        parts.append(tbl)

    if system_cmds:
        parts.append(Text.from_markup("[bold yellow]System[/]"))
        tbl = Table(
            show_header=True,
            header_style="bold cyan",
            box=table_box,
            padding=(0, 1, 0, 1),
            show_lines=False,
        )
        tbl.add_column("Command", style="cyan", width=18)
        tbl.add_column("Description", style="dim", width=60)
        for c in system_cmds:
            tbl.add_row(c["name"], c.get("description", ""))
        parts.append(tbl)

    return Group(*parts)


def show_commands_table(commands: list[dict], include_system: bool = True) -> None:
    """Display available commands grouped by category: compact, easy to scan."""
    subtitle = " (from Confluence)" if include_system and any(c["name"] not in _SYSTEM_CMD_NAMES for c in commands) else ""
    console.print(Text.from_markup(f"[bold cyan]Available Commands[/][dim]{subtitle}:[/]"))
    console.print(_build_commands_tables_renderable(commands, include_system))
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


def show_connected_device_banner(
    model: str | None,
    fw_version: str | None,
    env: str | None = None,
    connection_type: str = "",
    device_identifier: str = "",
    commands: list[dict] | None = None,
    include_system_commands: bool = True,
) -> None:
    """
    Display connected device info (model, FW, env) and optionally all commands
    inside a single "Connected camera" panel.
    """
    model_display = model or "—"
    fw_display = fw_version or "—"
    env_display = (env or "—").upper() if env else "—"
    info_lines = [
        f"[bold]Model[/]  [cyan]{model_display}[/]",
        f"[bold]FW[/]     [cyan]{fw_display}[/]",
        f"[bold]Env[/]    [cyan]{env_display}[/]",
    ]
    if connection_type:
        info_lines.append(f"[bold]Connection[/] [dim]{connection_type}[/] [dim]{device_identifier or ''}[/]")
    info_text = Text.from_markup("\n".join(info_lines))
    info_panel = Panel(
        info_text,
        title="[bold cyan]Device (build_info + kvcmd)[/]",
        border_style="cyan",
        box=ASCII_BOX,
        padding=(0, 1, 0, 1),
    )

    inner_parts: list = [
        Text.from_markup("[bold cyan]Connected camera[/]"),
        Text(),
        info_panel,
    ]
    if commands:
        inner_parts.append(Text())
        inner_parts.append(Text.from_markup("[bold cyan]Available Commands[/] [dim](from Confluence)[/]"))
        inner_parts.append(Text())
        inner_parts.append(_build_commands_tables_renderable(commands, include_system_commands, box_style=None))
        inner_parts.append(Text())
        inner_parts.append(Text.from_markup("[dim]Type [bold]help[/] to see this again.[/]"))

    banner = Panel(
        Group(*inner_parts),
        border_style="cyan",
        box=ASCII_BOX,
        padding=(0, 1, 1, 1),
    )
    console.print(banner)
    if not commands:
        console.print(Text.from_markup("[dim]Commands from Confluence (E3 Wired / arlochat). Type [bold]help[/] to see again.[/]\n"))


def show_success(message: str) -> None:
    """Print a success message with green checkmark."""
    console.print(f"[bold green]\u2713[/] [green]{message}[/]")


def show_error(message: str, suggestion: str | None = None) -> None:
    """Print an error message with red cross and optional suggestion."""
    console.print(f"[bold red]\u2717[/] [red]{message}[/]")
    if suggestion:
        console.print(f"[yellow]{suggestion}[/]")


def show_info(message: str) -> None:
    """Print an informational message (dim)."""
    console.print(f"[dim]{message}[/]")
