FROM golang:1.22-alpine AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /whatsapp-bridge .

FROM alpine:3.19
RUN apk add --no-cache ca-certificates
COPY --from=builder /whatsapp-bridge /usr/local/bin/whatsapp-bridge

# Auth state volume
VOLUME /data/auth

EXPOSE 8080

USER nobody
ENTRYPOINT ["whatsapp-bridge"]
CMD ["--listen", ":8080", "--auth-dir", "/data/auth"]
