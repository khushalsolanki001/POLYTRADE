"""Surgically fix lines 326-334 in bot.py for the send_message startup call."""
import sys

with open("bot.py", encoding="utf-8") as f:
    lines = f.readlines()

# Find the line containing "PolyTrack is online"
target_line = None
for i, line in enumerate(lines):
    if "PolyTrack is online" in line:
        target_line = i
        break

if target_line is None:
    sys.exit("ERROR: Could not find 'PolyTrack is online'")

sys.stdout.buffer.write(f"Found at line {target_line+1}: {repr(lines[target_line])}\n".encode())

# We'll search backwards for the "await app.bot.send_message(" line
send_line = None
for j in range(target_line, max(target_line-10, 0), -1):
    if "await app.bot.send_message(" in lines[j]:
        send_line = j
        break

if send_line is None:
    sys.exit("ERROR: Could not find send_message call")

# Search forward for the closing ")" of the send_message call
close_line = None
for j in range(target_line, min(target_line+15, len(lines))):
    stripped = lines[j].strip()
    if stripped == ")":
        close_line = j
        break

sys.stdout.buffer.write(f"send_line={send_line+1}, target={target_line+1}, close={close_line+1}\n".encode())

# Now we know the entire block is lines[send_line:close_line+1]
# Replace with the clean version
indent = "            "  # 12 spaces
clean_block = [
    indent + "await app.bot.send_message(\n",
    indent + "    chat_id=cid,\n",
    indent + '    text=(\n',
    indent + '        "\U0001f7e2 *PolyTrack is online" r"\\!*"\n',
    indent + '        r"\\nWallet monitoring has resumed\\."\n',
    indent + "    ),\n",
    indent + '    parse_mode="MarkdownV2",\n',
    indent + ")\n",
]

# Actually the simplest fix: just use a variable-based approach
# Replace everything from send_line to close_line with clean block
new_lines = lines[:send_line] + clean_block + lines[close_line+1:]

with open("bot.py", "w", encoding="utf-8") as f:
    f.writelines(new_lines)

sys.stdout.buffer.write(b"Written successfully!\n")
