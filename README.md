A feature-rich, asynchronous Telegram self-bot built with Telethon. It automates a user account with a wide array of powerful commands for administration, file management, AI interaction, and various utilities.

## Features

-   **Admin & Control**: Sudo/admin system, remote bot control (`on`, `off`, `restart`, `status`, `logs`) integrated with Supervisor.
-   **AI Integration**:
    -   Access ChatGPT for standard queries (`gpt`), web-enabled searches (`gpts`), and reasoning (`gptr`).
    -   Generate images from text prompts (`imagine`).
-   **File & Media Management**:
    -   Create and extract ZIP archives, with password protection (`zipfile`, `unzip`).
    -   Queue files for bulk zipping (`add`, `zipit`).
    -   Rename files and edit audio metadata (`rename`, `metadata`).
    -   Split PDF files by page range (`split`).
-   **Conversion Tools**:
    -   Convert text to speech (`tts`).
    -   Convert text messages to PDF documents with English and Persian support (`topdf`).
-   **Automation & Personalization**:
    -   **Quick Replies**: Set, manage, and trigger text shortcuts (`qreply`, `-alias`).
    -   **Auto-Reactions**: Automatically react with a specific emoji to new messages in designated channels (`setreact`).
    -   **Stickers**: Create text-based stickers on the fly (`stick`) and manage them in personal sticker packs (`stickerpack`).
-   **Search & Download**:
    -   Search Anna's Archive for books (`annas`) and articles (`art`).
    -   Download resources directly using a simple command (`dl_...`).
-   **Information & Utilities**:
    -   Fetch detailed user information, including profile pictures/videos (`info`).
    -   Get daily and hourly weather forecasts (`dw`, `hw`).
    -   Retrieve live currency, gold, and coin prices (`currency`).
    -   Look up word definitions with English/Persian meanings and pronunciation (`dic`).
    -   Generate and read QR codes with advanced color options (`qr`, `qradv`, `qrread`).
-   **Timers**: Set, view, and manage dynamic countdown timers with a rich, auto-updating display.

## Installation & Setup

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/ImTheAlireza/selfbot.git
    cd selfbot
    ```

2.  **Install Dependencies**
    Install all required Python packages. It is recommended to use a virtual environment.
    ```bash
    pip install telethon requests aiohttp pymysql pyzipper qrcode PyPDF2 arabic_reshaper python-bidi geopy beautifulsoup4 Pillow mutagen reportlab
    ```

3.  **Set Up Database**
    This bot requires a MySQL database. Create a database and a user with privileges to access it. The bot will automatically create the necessary tables on first run.

4.  **Configure the Bot**
    Open `self.py` and modify the following configuration sections with your own credentials. Do not share these values publicly.

    -   **`TELEGRAM_CONFIG`**:
        -   `api_id` & `api_hash`: Get these from [my.telegram.org](https://my.telegram.org).
        -   `phone_number`: Your Telegram account's phone number in international format.
    -   **`BOT_CONFIG`**:
        -   `sudo_user_id`: Your Telegram user ID. This grants you owner privileges.
    -   **`API_KEYS`**:
        -   `rapidapi`: Your key from [RapidAPI](https://rapidapi.com/).
    -   **`LOG_CHANNEL_ID`**: ID of the private channel where the bot will send logs (e.g., `-100123456789`).
    -   **Database Credentials**: Locate the `get_db_cursor` function and update the `pymysql.connect` parameters (`host`, `user`, `password`, `database`) with your MySQL details.
    -   **Supervisor (Optional)**: If you plan to use `self restart`, `self status`, or `self logs`, update the `SUPERVISOR_CONFIG` and `SUPERVISOR_PROCESS` paths.
    -   **`STICKER_BOT_TOKEN`**: To use the `stickerpack` feature, you must create a helper bot via `@BotFather` and provide its token here.

5.  **Run the Bot**
    -   **First Run**: Run the script directly to log in and create a `.session` file.
        ```bash
        python self.py
        ```
    -   **Deployment**: For continuous operation, it is highly recommended to run the bot using a process manager like `supervisor`.

## Disclaimer

Using a self-bot is a violation of Telegram's Terms of Service. This can lead to your account being limited or permanently banned. The author is not responsible for any consequences of using this software. **Use at your own risk.**

## Command Reference

### General & Control

| Command                               | Description                                                                                                   |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `help`                                | Displays the list of available commands.                                                                      |
| `self on`                             | Activates the bot.                                                                                            |
| `self off`                            | Deactivates the bot (except for the `self on` command).                                                       |
| `self restart`                        | **Sudo only.** Restarts the bot via Supervisor. Requires confirmation.                                        |
| `self status`                         | **Sudo only.** Checks the bot's status via Supervisor.                                                        |
| `self logs [n]`                       | **Sudo only.** Shows the last `n` lines of the error log (default: 20).                                        |
| `info [user_id / @username]`          | Retrieves detailed information about a user. Replying to a message also works.                              |
| `backup`                              | **Sudo only.** Backs up the script and database, sending the files to you.                                     |
| `dbupdate`                            | **Sudo only.** Imports a database from a replied `.sql` backup file.                                          |

### Admin Management (Sudo only)

| Command                  | Description                    |
| ------------------------ | ------------------------------ |
| `setadmin [user_id]`     | Adds a user as a bot admin.    |
| `remadmin [user_id]`     | Removes a user from the admins.  |
| `adminlist`              | Lists all sudo and admin users. |

### Messaging & Deletion

| Command                                      | Description                                                                                             |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `spam [message] [count]`                     | Sends a message multiple times.                                                                         |
| `cancel`                                     | Stops the current spam task.                                                                            |
| `spamset [delay/limit/cooldown] [value]`     | **Sudo only.** Configures spam settings. `delay` is in milliseconds.                                    |
| `del [count]`                                | Deletes the last `count` messages.                                                                      |
| `del [type]`                                 | Deletes messages of a specific type. Types: `photos`, `videos`, `voices`, `musics`, `files`, `all`, etc. |

### Automation & Personalization

| Command                               | Description                                                                                                                                                                 |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `qreply set [alias] [message]`        | Creates or updates a quick reply. Can also be used by replying to a message: `qreply set [alias]`.                                                                          |
| `qreply remove [alias]`               | Deletes a quick reply.                                                                                                                                                      |
| `qreply list`                         | Lists all your quick replies.                                                                                                                                               |
| `-[alias]`                            | Triggers a quick reply by editing your message.                                                                                                                             |
| `setreact @channel [emoji]`           | **Sudo only.** Sets an emoji to automatically react to new messages in a channel.                                                                                         |
| `remreact @channel`                   | **Sudo only.** Removes an auto-reaction for a channel.                                                                                                                    |
| `reactlist`                           | **Sudo only.** Lists all configured auto-reactions.                                                                                                                       |

### Timers

| Command                           | Description                                                               |
| --------------------------------- | ------------------------------------------------------------------------- |
| `settimer [duration] [title]`     | Sets a new countdown timer. Duration format: `HH:MM:SS` or `MM:SS`.       |
| `activetimers`                    | Lists all currently active timers.                                        |
| `dismiss [hash]`                  | Dismisses and deletes an active timer.                                    |
| `resend [hash]`                   | Resends the timer message to the current chat.                            |

### File & Media Tools

| Command                           | Description                                                              |
| --------------------------------- | ------------------------------------------------------------------------ |
| `zipfile [password]`              | Zips the replied file. Password is optional.                             |
| `unzip [password]`                | Unzips the replied ZIP file. Password is required for protected archives. |
| `add`                             | Adds the replied file to a queue for zipping.                            |
| `zipit [password]`                | Zips all files in the queue.                                             |
| `rename [new_name]`               | Renames the replied file.                                                |
| `metadata [title] - [artist]`     | Edits the title and artist metadata of a replied audio file.             |
| `split [start-end]`               | Splits a replied PDF from a start page to an end page.                   |

### AI, Search, & Utilities

| Command                           | Description                                                               |
| --------------------------------- | ------------------------------------------------------------------------- |
| `gpt [prompt]`                    | Gets a response from ChatGPT.                                             |
| `gpts [prompt]`                   | Gets a response from ChatGPT with web search access.                     |
| `gptr [prompt]`                   | Gets a response using GPT's reasoning mode.                               |
| `imagine [prompt]`                | Generates an image based on the text prompt.                              |
| `tts`                             | Converts the text of a replied message to speech.                         |
| `topdf en/fa [size]`              | Converts replied text to a PDF. Language and font size are optional.      |
| `dw [city]`                       | Gets the daily weather forecast for a city.                               |
| `hw [city]`                       | Gets the hourly weather forecast for a city.                              |
| `currency`                        | Fetches live prices for currencies, gold, and coins.                      |
| `dic [word]`                      | Looks up a word's definition, pronunciation, and Persian translation.       |
| `annas [query]`                   | Searches Anna's Archive for books.                                        |
| `art [query]`                     | Searches Anna's Archive for articles.                                     |
| `dl_[md5_hash]`                   | Downloads a book/article using its MD5 hash from a search result.         |

### Sticker Tools

| Command                                   | Description                                                                          |
| ----------------------------------------- | ------------------------------------------------------------------------------------ |
| `stick [text]`                            | Creates a sticker from the provided text.                                            |
| `stick -save [text]`                      | Creates a sticker and adds it to the currently open pack.                            |
| `stickerpack create [name] [title]`       | Initializes a new sticker pack. Not created until the first sticker is saved.    |
| `stickerpack open [name]`                 | Opens an existing pack to add more stickers to it.                                   |
| `stickerpack list`                        | Lists all your created sticker packs with links.                                     |
| `stickerpack delete [name]`               | Deletes a sticker pack from Telegram and the database.                               |
| `stickerpack close`                       | Closes the currently active pack.                                                    |

### QR Code Tools

| Command                            | Description                                                              |
| ---------------------------------- | ------------------------------------------------------------------------ |
| `qr [text] [size]`                 | Generates a QR code. Size is optional (default: 10).                     |
| `qradv [text] [fg_color] [bg_color]` | Generates a QR code with custom foreground and background colors.        |
| `qrread`                           | Reads the QR code from a replied image.                                  |
