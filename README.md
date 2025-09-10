# gptcli — gpt-5-mini をターミナルから使う軽量 CLI

## インストール

```
pipx install git+https://github.com/tower2149/gptcli.git
```

## セットアップ（API キー）

```
export OPENAI_API_KEY="sk-..."
# 必要なら
# export OPENAI_BASE_URL="https://api.openai.com/v1"
# export OPENAI_MODEL="gpt-4o-mini"
```
環境変数は gpt 起動時に読み込まれます。

## 使い方

```
gpt "この資料を要約して" --upload report.pdf
```

## ライセンス

MIT License

## 謝辞

- 本ツールは OpenAI API を利用します。各サービスの利用規約に従ってお使いください。
