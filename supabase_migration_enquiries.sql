-- ─── Enquiries Table ──────────────────────────────────────────────────────────
-- Run this in your Supabase project: Dashboard → SQL Editor → New query → Run

CREATE TABLE IF NOT EXISTS public.enquiries (
    id                bigserial PRIMARY KEY,
    created_at        timestamptz DEFAULT now() NOT NULL,
    caller_name       text DEFAULT '',
    caller_phone      text DEFAULT '',
    property_type     text DEFAULT '',
    location          text DEFAULT '',
    budget            text DEFAULT '',
    purpose           text DEFAULT '',
    requirements      text DEFAULT '',
    call_id           text DEFAULT '',
    booking_confirmed boolean DEFAULT false,
    booking_datetime  timestamptz DEFAULT NULL
);

-- Enable Row Level Security (recommended)
ALTER TABLE public.enquiries ENABLE ROW LEVEL SECURITY;

-- Allow service-role key full access (used by Python backend)
CREATE POLICY "service_role_all" ON public.enquiries
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Index for fast lookups by phone
CREATE INDEX IF NOT EXISTS enquiries_phone_idx    ON public.enquiries (caller_phone);
CREATE INDEX IF NOT EXISTS enquiries_created_idx  ON public.enquiries (created_at DESC);
CREATE INDEX IF NOT EXISTS enquiries_booking_idx  ON public.enquiries (booking_confirmed);


-- Done ✅
SELECT 'enquiries table created successfully' AS status;
