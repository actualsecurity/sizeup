// The station's trunk-recorder patch (#638, deploy/station/install-linux.sh tr_patch) on the real transmission_sink:
// a vector source with the tags trunk-recorder's P25 frame assembler puts on its audio ("src_id", "grp_id",
// "terminate") feeds the sink in fixed windows; each case checks the transmissions it made (radio ID, samples, the WAV
// on disk) and, for SIZEUP_TR_SPLIT=log, what it logged. Built against the trunk-recorder source tree and its
// libtrunk-recorder.a by .github/scripts/tr-split-test.sh. Synthetic audio, invented radio IDs.
#include "trunk-recorder/call.h"
#include "trunk-recorder/global_structs.h"
#include "trunk-recorder/gr_blocks/transmission_sink.h"

#include <boost/core/null_deleter.hpp>
#include <boost/log/core.hpp>
#include <boost/log/sinks/sync_frontend.hpp>
#include <boost/log/sinks/text_ostream_backend.hpp>
#include <boost/make_shared.hpp>
#include <gnuradio/blocks/vector_source.h>
#include <gnuradio/top_block.h>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <vector>

static std::string temp_dir;
static std::ostringstream logged;

class StubCall : public Call {
public:
  bool conventional;
  long grant_src;
  StubCall(bool c, long g) : conventional(c), grant_src(g) {}
  long get_call_num() override { return 1; }
  void restart_call() override {  }
  void stop_call() override {  }
  void conclude_call() override {  }
  void set_sigmf_recorder(Recorder *r) override {  }
  Recorder * get_sigmf_recorder() override { return {}; }
  void set_debug_recorder(Recorder *r) override {  }
  Recorder * get_debug_recorder() override { return {}; }
  void set_recorder(Recorder *r) override {  }
  Recorder * get_recorder() override { return {}; }
  double get_freq() override { return 770181250.0; }
  int get_sys_num() override { return {}; }
  std::string get_short_name() override { return "test"; }
  std::string get_capture_dir() override { return temp_dir; }
  std::string get_temp_dir() override { return temp_dir; }
  void set_freq(double f) override {  }
  long get_talkgroup() override { return 9211; }
  bool update(TrunkMessage message) override { return {}; }
  int get_idle_count() override { return {}; }
  void increase_idle_count() override {  }
  void reset_idle_count() override {  }
  int since_last_update() override { return {}; }
  double since_last_voice_update() override { return {}; }
  long elapsed() override { return {}; }
  double get_current_length() override { return {}; }
  long get_stop_time() override { return {}; }
  void set_debug_recording(bool m) override {  }
  bool get_debug_recording() override { return {}; }
  void set_sigmf_recording(bool m) override {  }
  bool get_sigmf_recording() override { return {}; }
  void set_state(State s) override {  }
  State get_state() override { return {}; }
  void set_monitoring_state(MonitoringState s) override {  }
  MonitoringState get_monitoring_state() override { return {}; }
  void set_phase2_tdma(bool m) override {  }
  bool get_phase2_tdma() override { return {}; }
  void set_tdma_slot(int s) override {  }
  int get_tdma_slot() override { return {}; }
  bool get_is_analog() override { return {}; }
  void set_is_analog(bool a) override {  }
  const char * get_xor_mask() override { return {}; }
  time_t get_start_time() override { return {}; }
  std::int64_t get_start_time_ms() override { return {}; }
  bool is_conventional() override { return conventional; }
  void set_encrypted(bool m) override {  }
  bool get_encrypted() override { return {}; }
  void set_emergency(bool m) override {  }
  bool get_emergency() override { return {}; }
  int get_priority() override { return {}; }
  bool get_mode() override { return {}; }
  bool get_duplex() override { return {}; }
  double get_signal() override { return {}; }
  double get_noise() override { return {}; }
  int get_freq_error() override { return {}; }
  void set_signal(double s) override {  }
  void set_noise(double n) override {  }
  std::string get_talkgroup_display() override { return "9211"; }
  void set_talkgroup_tag(std::string tag) override {  }
  void clear_transmission_list() override {  }
  boost::property_tree::ptree get_stats() override { return {}; }
  std::string get_talkgroup_tag() override { return {}; }
  std::string get_system_type() override { return "p25"; }
  double get_final_length() override { return {}; }
  long get_current_source_id() override { return grant_src; }
  bool get_conversation_mode() override { return {}; }
  System * get_system() override { return {}; }
  std::vector<Transmission> get_transmissions() override { return {}; }
};

struct Tag {
  long at;
  const char *key;
  long value;
};

struct Got {
  std::vector<Transmission> txs;
  std::string log;
};

static const long RA = 9900101, RA2 = 9900111, RB = 9900202, RV = 9900303, RG = 9900909; // invented radio IDs
static const int W = 200; // samples per work() call

static Got run(const char *mode, bool conventional, long grant, int n, const std::vector<Tag> &tags) {
  setenv("SIZEUP_TR_SPLIT", mode, 1);
  logged.str("");
  StubCall call(conventional, grant);
  gr::blocks::transmission_sink::sptr sink = gr::blocks::transmission_sink::make(1, 8000, 16);
  sink->start_recording(&call);
  std::vector<short> data(n);
  for (int i = 0; i < n; i++) {
    data[i] = (short)(1000 + i % 100); // never silent: every sample counts
  }
  std::vector<gr::tag_t> gtags;
  for (const Tag &t : tags) {
    gr::tag_t g;
    g.offset = t.at;
    g.key = pmt::intern(t.key);
    g.value = pmt::from_long(t.value);
    g.srcid = pmt::intern("test");
    gtags.push_back(g);
  }
  gr::blocks::vector_source_s::sptr src = gr::blocks::vector_source_s::make(data, false, 1, gtags);
  sink->set_output_multiple(W);
  sink->set_max_noutput_items(W);
  gr::top_block_sptr tb = gr::make_top_block("tr_split_test");
  tb->connect(src, 0, sink, 0);
  tb->run(W);
  sink->stop_recording();
  Got got;
  got.txs = sink->get_transmission_list();
  got.log = logged.str();
  return got;
}

static int failures = 0;

static void expect(const char *name, const Got &got, const std::vector<std::pair<long, long>> &want,
                   const char *log_has = nullptr, const char *log_lacks = nullptr) {
  bool ok = got.txs.size() == want.size();
  for (size_t i = 0; ok && i < want.size(); i++) {
    ok = got.txs[i].source == want[i].first && got.txs[i].sample_count == want[i].second;
    struct stat st;
    ok = ok && stat(got.txs[i].filename.c_str(), &st) == 0 && st.st_size == 44 + 2 * want[i].second;
  }
  if (log_has && got.log.find(log_has) == std::string::npos) {
    ok = false;
  }
  if (log_lacks && got.log.find(log_lacks) != std::string::npos) {
    ok = false;
  }
  std::cout << (ok ? "ok    " : "FAIL  ") << name << ":";
  for (const Transmission &t : got.txs) {
    std::cout << " [" << t.source << " " << t.sample_count << "]";
  }
  std::cout << std::endl;
  if (!ok) {
    failures++;
    std::cout << "  want:";
    for (const auto &w : want) {
      std::cout << " [" << w.first << " " << w.second << "]";
    }
    std::cout << std::endl << got.log << std::endl;
  }
}

// a talker the voice channel names every 2880 samples (360 ms: P25 Phase 1's LDU1), from `from` up to `to`
static void every(std::vector<Tag> &tags, long src, long from, long to) {
  for (long at = from; at < to; at += 2880) {
    tags.push_back({at, "src_id", src});
  }
}

int main() {
  char tmpl[] = "/tmp/tr-split-test-XXXXXX";
  temp_dir = mkdtemp(tmpl);
  boost::shared_ptr<boost::log::sinks::text_ostream_backend> backend = boost::make_shared<boost::log::sinks::text_ostream_backend>();
  backend->add_stream(boost::shared_ptr<std::ostream>(&logged, boost::null_deleter()));
  boost::log::core::get()->add_sink(boost::make_shared<boost::log::sinks::synchronous_sink<boost::log::sinks::text_ostream_backend>>(backend));

  std::vector<Tag> t;

  // A talks, B keys up mid-window with no terminator between: cut where B's ID came in
  t.clear(); every(t, RA, 0, 10000); every(t, RB, 10050, 20000);
  expect("split mid-window", run("on", false, RA, 20000, t), {{RA, 10050}, {RB, 9950}}, "Unit ID changed on Voice Channel, ext: 9900202");
  // the same input, stock and log: one transmission credited to the later radio; log says where it would cut
  expect("off: stock", run("off", false, RA, 20000, t), {{RB, 20000}}, nullptr, "would split");
  expect("log: stock, logged", run("log", false, RA, 20000, t), {{RB, 20000}}, "would split: prev 9900101 -> new 9900202, 10050 samples into transmission");

  // B's ID and a terminator in one window, B first: A to B's ID, B to the window's end (B heard once, then the end)
  t.clear(); every(t, RA, 0, 10000); t.push_back({10050, "src_id", RB}); t.push_back({10120, "terminate", 1});
  expect("new ID then terminate, one window", run("on", false, RA, 10200, t), {{RA, 10050}, {RB, 150}});
  // the terminator first: A's last chunk runs to B's ID, then B is a transmission of its own
  t.clear(); every(t, RA, 0, 10000); t.push_back({10020, "terminate", 1}); t.push_back({10050, "src_id", RB});
  expect("terminate then new ID, one window", run("on", false, RA, 12000, t), {{RA, 10050}, {RB, 1950}});
  // a terminator at a window's start, the next talker in the next window
  t.clear(); every(t, RA, 0, 10000); t.push_back({10000, "terminate", 1}); every(t, RB, 10200, 14000);
  expect("terminate then new ID, next window", run("on", false, RA, 14000, t), {{RA, 10200}, {RB, 3800}});

  // the same radio all along: one transmission
  t.clear(); every(t, RA, 0, 20000);
  expect("same ID again", run("on", false, RA, 20000, t), {{RA, 20000}});
  // the grant named one radio, the voice channel another: replaced (stock), never cut
  t.clear(); every(t, RV, 400, 12000);
  expect("grant-only ID replaced", run("on", false, RG, 12000, t), {{RV, 12000}}, nullptr, "Unit ID changed");
  // a new ID heard once, then nothing for a second: the new talker from where it came in
  t.clear(); every(t, RA, 0, 10000); t.push_back({10050, "src_id", RB});
  expect("new ID held, then settled", run("on", false, RA, 20000, t), {{RA, 10050}, {RB, 9950}});
  // a roaming radio's two IDs: a flip back to the first is the same talker, never a cut
  t.clear(); every(t, RA, 0, 10000); t.push_back({10050, "src_id", RA2}); t.push_back({10500, "src_id", RA});
  t.push_back({13000, "src_id", RA2}); t.push_back({15000, "src_id", RA});
  expect("ID flip back", run("on", false, RA, 20000, t), {{RA, 20000}}, "ID flip 9900111 ~ 9900101", "Unit ID changed");
  // a second ID early in the turn (within a second): the same key-up
  t.clear(); t.push_back({0, "src_id", RA}); t.push_back({2000, "src_id", RA2}); t.push_back({4880, "src_id", RA2});
  t.push_back({7000, "src_id", RA});
  expect("early second ID", run("on", false, RA, 12000, t), {{RA, 12000}}, "ID alias", "Unit ID changed");

  // another talkgroup's voice: A ends there, the rest is dropped until a terminator, then B
  t.clear(); every(t, RA, 0, 9000); t.push_back({4000, "grp_id", 9211}); t.push_back({9050, "grp_id", 4321});
  t.push_back({12000, "terminate", 1}); every(t, RB, 12200, 16000);
  expect("group mismatch", run("on", false, RA, 16000, t), {{RA, 9050}, {RB, 3800}});
  // a conventional channel keeps its own (stock) handling
  t.clear(); every(t, RA, 0, 10000); every(t, RB, 10050, 20000);
  expect("conventional: stock", run("on", true, RA, 20000, t), {{RB, 20000}}, nullptr, "Unit ID changed");

  std::cout << (failures ? "FAILED" : "all ok") << " (" << failures << " failed)" << std::endl;
  return failures ? 1 : 0;
}
