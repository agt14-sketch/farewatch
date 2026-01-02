import datetime
import requests
import streamlit as st

DEFAULT_API_BASE = "http://127.0.0.1:8000"


# -----------------------------
# HTTP helpers
# -----------------------------
def api_get(path: str, api_base: str, params: dict | None = None):
    r = requests.get(f"{api_base}{path}", params=params, timeout=30)
    is_json = r.headers.get("content-type", "").startswith("application/json")
    return r.status_code, (r.json() if is_json else r.text)


def api_post(path: str, api_base: str, payload: dict):
    r = requests.post(f"{api_base}{path}", json=payload, timeout=60)
    is_json = r.headers.get("content-type", "").startswith("application/json")
    return r.status_code, (r.json() if is_json else r.text)


def api_delete(path: str, api_base: str, payload: dict):
    # FastAPI DELETE can accept JSON body if you coded it that way (you did).
    r = requests.delete(f"{api_base}{path}", json=payload, timeout=30)
    is_json = r.headers.get("content-type", "").startswith("application/json")
    return r.status_code, (r.json() if is_json else r.text)


def money(cents):
    return "—" if cents is None else f"${cents/100:.2f}"


# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="DaiLY (Testing)", page_icon="✈️", layout="centered")
st.title("✈️ DaiLY (Testing)")

api_base = st.sidebar.text_input("API base", value=DEFAULT_API_BASE).strip()
st.sidebar.caption("Make sure your FastAPI server is running here.")


tab_search, tab_create, tab_my, tab_unsub = st.tabs(
    ["Search", "Create Watch", "My Watches", "Unsubscribe"]
)

# -----------------------------
# TAB: Search
# -----------------------------
with tab_search:
    st.subheader("Search flights (any cabin)")

    with st.form("search_form"):
        c1, c2 = st.columns(2)
        with c1:
            origin = st.text_input("Origin (IATA)", value="BWI").upper().strip()
            depart_date = st.date_input(
                "Departure date",
                value=datetime.date.today() + datetime.timedelta(days=20),
            )
            adults = st.number_input("Adults", min_value=1, max_value=9, value=1, step=1)
        with c2:
            destination = st.text_input("Destination (IATA)", value="SFO").upper().strip()
            cabin = st.selectbox(
                "Cabin",
                options=["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"],
                index=0,
            )
            currency = st.text_input("Currency", value="USD").upper().strip()

        max_price = st.number_input("Max price (optional)", min_value=0.0, value=0.0, step=50.0)
        max_results = st.slider("Max results", 1, 30, 10)

        go = st.form_submit_button("Search")

    if go:
        payload = {
            "origin": origin,
            "destination": destination,
            "depart_date": depart_date.strftime("%Y-%m-%d"),
            "adults": int(adults),
            "cabin": cabin,
            "currency": currency,
            "max_price": max_price if max_price > 0 else None,
            "max_results": int(max_results),
        }

        status, data = api_post("/search", api_base, payload)

        if status != 200:
            st.error(f"Search failed (status {status})")
            st.write(data)
        else:
            offers = data.get("offers", [])
            if not offers:
                st.warning("No offers found.")
            else:
                rows = [
                    {
                        "Carrier": o.get("carrier"),
                        "Total": o.get("total"),
                        "Currency": o.get("currency"),
                        "Segments": o.get("segments"),
                        "Duration": o.get("duration"),
                    }
                    for o in offers
                ]
                st.success(f"Found {len(rows)} offers")
                st.dataframe(rows, use_container_width=True)


# -----------------------------
# TAB: Create Watch
# -----------------------------
with tab_create:
    st.subheader("Create a Watch (and optionally subscribe yourself)")

    with st.form("create_watch_form"):
        c1, c2, c3 = st.columns(3)
        origin = c1.text_input("Origin", value="BWI").upper().strip()
        destination = c2.text_input("Destination", value="SFO").upper().strip()
        depart_date = c3.date_input(
            "Depart date",
            value=datetime.date.today() + datetime.timedelta(days=30),
        )

        c4, c5, c6 = st.columns(3)
        adults = c4.number_input("Adults", 1, 9, 1)
        cabin = c5.selectbox("Cabin", ["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"], index=0)
        currency = c6.text_input("Currency", value="USD").upper().strip()

        alert_email = st.text_input("Your email for alerts (optional)", value="").strip()

        submit = st.form_submit_button("Create watch")

    if submit:
        payload = {
            "origin": origin,
            "destination": destination,
            "depart_date": depart_date.strftime("%Y-%m-%d"),
            "adults": int(adults),
            "cabin": cabin,
            "currency": currency,
            "alert_email": alert_email if alert_email else None,
        }

        status, data = api_post("/watches", api_base, payload)
        if status != 200:
            st.error(f"Create watch failed (status {status})")
            st.write(data)
        else:
            st.success(f"Watch created/reused! watch_id={data.get('watch_id')}")
            st.json(data)


# -----------------------------
# TAB: My Watches
# -----------------------------
with tab_my:
    st.subheader("My Watches (all watches in DB)")

    if st.button("Refresh watches"):
        st.session_state["refresh_watches"] = True

    status, data = api_get("/watches", api_base)
    if status != 200:
        st.error(f"Failed to load watches (status {status})")
        st.write(data)
    else:
        watches = data.get("watches", [])
        if not watches:
            st.info("No watches yet. Create one first.")
        else:
            st.caption("Tip: expand a watch to add/view subscribers.")
            for w in watches:
                wid = w.get("id") or w.get("watch_id")
                title = f"#{wid} {w['origin']}→{w['destination']} {w['depart_date']} | {w.get('cabin','ECONOMY')} | {w.get('adults',1)} adult(s)"

                with st.expander(title, expanded=False):
                    st.write(
                        f"Snapshots: **{w.get('n', 0)}**  \n"
                        f"Latest: **{money(w.get('latest_cents'))}**  \n"
                        f"Min: **{money(w.get('min_cents'))}**  \n"
                        f"Median: **{money(w.get('median_cents'))}**"
                    )

                    st.divider()
                    st.markdown("### Add a friend (subscribe)")

                    friend_email = st.text_input(
                        "Friend email",
                        key=f"friend_email_{wid}",
                        placeholder="friend@example.com",
                    ).strip()

                    if st.button("Subscribe friend", key=f"btn_sub_{wid}"):
                        if not friend_email:
                            st.warning("Enter an email first.")
                        else:
                            s2, d2 = api_post(
                                "/subscriptions",
                                api_base,
                                {"watch_id": int(wid), "email": friend_email},
                            )
                            if s2 == 200:
                                st.success(f"Subscribed {friend_email} to watch {wid}")
                            else:
                                st.error(f"Subscribe failed (status {s2})")
                                st.write(d2)

                    st.divider()
                    st.markdown("### Subscribers")

                    if st.button("Load subscribers", key=f"btn_loadsubs_{wid}"):
                        s3, d3 = api_get(f"/watches/{wid}/subscriptions", api_base)
                        if s3 == 200:
                            subs = d3.get("subscriptions", d3)
                            if not subs:
                                st.info("No subscribers yet.")
                            else:
                                # show just email + last emailed
                                rows = []
                                for sub in subs:
                                    rows.append(
                                        {
                                            "Subscription ID": sub.get("id"),
                                            "Email": sub.get("email"),
                                            "Last emailed (cents)": sub.get("last_emailed_cents"),
                                            "Last emailed time": sub.get("last_emailed_seen_utc"),
                                        }
                                    )
                                st.dataframe(rows, use_container_width=True)
                        else:
                            st.error(f"Failed to load subscribers (status {s3})")
                            st.write(d3)


# -----------------------------
# TAB: Unsubscribe
# -----------------------------
with tab_unsub:
    st.subheader("Unsubscribe from a watch")

    watch_id = st.number_input("Watch ID", min_value=1, value=1, step=1)
    email = st.text_input("Email to remove", value="").strip()

    if st.button("Unsubscribe"):
        if not email:
            st.warning("Enter an email.")
        else:
            status, data = api_delete(
                "/subscriptions",
                api_base,
                {"watch_id": int(watch_id), "email": email},
            )
            if status == 200:
                deleted = data.get("deleted", 0)
                if deleted:
                    st.success(f"Unsubscribed {email} from watch {watch_id}")
                else:
                    st.info("No subscription found for that email + watch_id.")
            else:
                st.error(f"Unsubscribe failed (status {status})")
                st.write(data)

st.caption(
    "Heads up: Streamlit only *creates* watches/subscriptions. "
    "Emails are sent when you run your snapshot/scheduler script."
)