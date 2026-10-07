"""A consistent dark ttk theme, including macOS where native buttons ignore colours."""
BG = '#1e1e2e'
PANEL = '#313244'
CARD = '#252536'
TEXT = '#cdd6f4'
MUTED = '#9399b2'
BLUE = '#89b4fa'
GREEN = '#a6e3a1'
PURPLE = '#cba6f7'
PEACH = '#fab387'
RED = '#f38ba8'
TEAL = '#94e2d5'


def apply(root, ttk):
    import tkinter.font as font
    root.configure(background=BG)
    families = font.families(root)
    family = next((name for name in ('Arial', 'DejaVu Sans', 'Helvetica') if name in families), 'TkDefaultFont')
    root.option_add('*Background', BG)
    root.option_add('*Foreground', TEXT)
    root.option_add('*TCombobox*Listbox.background', PANEL)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', '#454f73')
    root.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=BG, foreground=TEXT, font=(family, 11), bordercolor=PANEL,
                    lightcolor=PANEL, darkcolor=PANEL, troughcolor=CARD)
    style.configure('TFrame', background=BG)
    style.configure('Card.TFrame', background=CARD)
    style.configure('TLabelframe', background=BG, bordercolor=PANEL)
    style.configure('TLabelframe.Label', foreground=BLUE, font=(family, 11, 'bold'))
    style.configure('TLabel', background=BG, foreground=TEXT)
    style.configure('Title.TLabel', foreground=BLUE, font=(family, 22, 'bold'))
    style.configure('Heading.TLabel', foreground=BLUE, font=(family, 12, 'bold'))
    style.configure('Muted.TLabel', foreground=MUTED)
    style.configure('Success.TLabel', foreground=GREEN, font=(family, 11, 'bold'))
    style.configure('Activity.TLabel', foreground=BLUE, font=(family, 11, 'bold'))
    style.configure('Scene.TLabel', background=PANEL, foreground=TEXT, padding=12, font=(family, 12))
    style.configure('TButton', background=PANEL, foreground=TEXT, padding=(12, 8), borderwidth=0)
    style.map('TButton', background=[('disabled', CARD), ('active', '#45475a')],
              foreground=[('disabled', '#6c7086')])
    for name, color in [('Blue', BLUE), ('Green', GREEN), ('Purple', PURPLE), ('Peach', PEACH)]:
        style.configure(name + '.TButton', background=color, foreground=BG, font=(family, 11, 'bold'))
        style.map(name + '.TButton', background=[('disabled', PANEL), ('active', TEXT)],
                  foreground=[('disabled', '#6c7086'), ('active', BG)])
    style.configure('TEntry', fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, padding=5)
    style.map('TEntry', fieldbackground=[('disabled', CARD)], foreground=[('disabled', MUTED)])
    style.configure('TCombobox', fieldbackground=PANEL, background=PANEL, arrowcolor=BLUE, padding=5)
    style.map('TCombobox', fieldbackground=[('readonly', PANEL), ('disabled', CARD)],
              foreground=[('disabled', MUTED), ('readonly', TEXT)], selectbackground=[('readonly', PANEL)],
              selectforeground=[('readonly', TEXT)])
    style.configure('TCheckbutton', background=BG, foreground=TEXT, padding=(0, 3))
    style.map('TCheckbutton', background=[('active', BG)], foreground=[('disabled', '#6c7086')])
    for name, color in [('Blue', BLUE), ('Purple', PURPLE), ('Green', GREEN), ('Peach', PEACH), ('Teal', TEAL), ('Muted', MUTED)]:
        style.configure(name + '.TRadiobutton', background=BG, foreground=color, padding=(0, 5))
        style.map(name + '.TRadiobutton', background=[('active', BG)], foreground=[('disabled', '#6c7086')])
    style.configure('Treeview', background=CARD, fieldbackground=CARD, foreground=TEXT,
                    rowheight=32, borderwidth=0)
    style.configure('Treeview.Heading', background=PANEL, foreground=BLUE,
                    font=(family, 11, 'bold'), padding=7, relief='flat')
    style.map('Treeview', background=[('selected', '#3c5480')], foreground=[('selected', '#ffffff')])
    style.map('Treeview.Heading', background=[('active', '#45475a')])
    style.configure('Horizontal.TProgressbar', background=BLUE, troughcolor=CARD, borderwidth=0, thickness=8)
    style.configure('TScrollbar', background=PANEL, arrowcolor=MUTED, borderwidth=0)
    style.configure('TNotebook', background=BG, borderwidth=0)
    style.configure('TNotebook.Tab', background=PANEL, foreground=MUTED, padding=(14, 6))
    style.map('TNotebook.Tab', background=[('selected', '#3c5480'), ('active', '#45475a')],
              foreground=[('selected', '#ffffff'), ('active', TEXT)])
    return style
